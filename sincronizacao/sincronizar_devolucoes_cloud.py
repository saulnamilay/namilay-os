"""
Namilay OS - Sincronizacao de Devolucoes (Tiny ERP -> banco na nuvem/Supabase)
------------------------------------------------------------------------------
Grava direto no banco de dados na nuvem (Postgres/Supabase).

COMO USAR:
  O token do Tiny fica no config_banco.py (TOKEN_TINY). Rode: python sincronizar_devolucoes_cloud.py
"""

import requests
import psycopg2
import os
import time
from datetime import datetime, timedelta, timezone
# Configuracao: variaveis de ambiente (GitHub Actions) ou config_banco.py (uso no seu PC)
DATABASE_URL = os.environ.get("DATABASE_URL")
TOKEN = os.environ.get("TOKEN_TINY")
if not DATABASE_URL or not TOKEN:
    try:
        import config_banco
        DATABASE_URL = DATABASE_URL or getattr(config_banco, "DATABASE_URL", None)
        TOKEN = TOKEN or getattr(config_banco, "TOKEN_TINY", None)
    except ImportError:
        pass
TOKEN = TOKEN or "COLE_SEU_TOKEN_AQUI"

FUSO_BRASIL = timezone(timedelta(hours=-3))   # Brasil nao tem horario de verao desde 2019


def agora_brasil():
    """Hora atual do Brasil (sem fuso na data), mesmo rodando num servidor em UTC."""
    return datetime.now(FUSO_BRASIL).replace(tzinfo=None)

# ============ PREENCHA/AJUSTE AQUI ============
# (o TOKEN do Tiny fica no config_banco.py, na linha: TOKEN_TINY = "seu_token")
DIAS_PARA_BUSCAR = 30
MARGEM_DIAS_REPROCESSAMENTO = 1
CHAMADAS_POR_MINUTO = 20   # parte da cota do Tiny, para nao brigar com a sincronizacao de vendas
FALHAS_SEGUIDAS_PARA_PARAR = 3
CODIGOS_FINALIDADE_DEVOLUCAO = {"4", "7", "8"}
# ================================================

URL_PESQUISA = "https://api.tiny.com.br/api2/notas.fiscais.pesquisa.php"
URL_DETALHE = "https://api.tiny.com.br/api2/nota.fiscal.obter.php"

INTERVALO_ENTRE_CHAMADAS = 60 / CHAMADAS_POR_MINUTO


# ---------------------------------------------------------------------------
# Chamadas a API do Tiny (com tolerancia a falhas)
# ---------------------------------------------------------------------------

def requisicao_resiliente(url, payload, tentativas=4, espera_entre_tentativas=10):
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            resposta = requests.post(url, data=payload, timeout=20)
            dados = resposta.json()
        except (requests.exceptions.RequestException, ValueError) as erro:
            ultimo_erro = erro
            print(f"    (falha de conexao, tentativa {tentativa}/{tentativas}: {erro})")
            if tentativa < tentativas:
                time.sleep(espera_entre_tentativas)
            continue

        retorno = dados.get("retorno", {}) if isinstance(dados, dict) else {}
        if retorno.get("status") == "Erro" and "API Bloqueada" in str(retorno.get("erros", "")):
            ultimo_erro = "API do Tiny bloqueada por excesso de acessos"
            espera = 60 * tentativa
            print(f"    (Tiny bloqueou por excesso de acessos -- aguardando {espera}s, "
                  f"tentativa {tentativa}/{tentativas})")
            if tentativa < tentativas:
                time.sleep(espera)
            continue

        return dados

    print(f"    Desisti depois de {tentativas} tentativas. Pulando este item.")
    return {"retorno": {"status": "Erro", "erros": [{"erro": str(ultimo_erro)}]}}


# ---------------------------------------------------------------------------
# Conexao com o banco na nuvem (com reconexao automatica)
# ---------------------------------------------------------------------------

def conectar():
    return psycopg2.connect(
        DATABASE_URL,
        connect_timeout=15,
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=5,
    )


def conectar_com_tentativas(tentativas=5, espera=10):
    ultimo_erro = None
    for tentativa in range(1, tentativas + 1):
        try:
            return conectar()
        except psycopg2.OperationalError as erro:
            ultimo_erro = erro
            print(f"    (nao consegui conectar ao banco na nuvem, tentativa {tentativa}/{tentativas})")
            if tentativa < tentativas:
                time.sleep(espera)
    raise ultimo_erro


def com_reconexao(conexao, operacao, *args, tentativas=3):
    for tentativa in range(1, tentativas + 1):
        try:
            return conexao, operacao(conexao, *args)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as erro:
            resumo = " ".join(str(erro).split())[:80]
            print(f"    (conexao com o banco caiu: {resumo} -- reconectando, "
                  f"tentativa {tentativa}/{tentativas})")
            try:
                conexao.close()
            except Exception:
                pass
            time.sleep(5 * tentativa)
            conexao = conectar_com_tentativas()
    print("    Nao consegui concluir essa operacao no banco depois de varias tentativas.")
    return conexao, None


# ---------------------------------------------------------------------------
# Controle do "ponto de partida" da sincronizacao incremental
# ---------------------------------------------------------------------------

def obter_controle(conexao, chave):
    cursor = conexao.cursor()
    cursor.execute("SELECT valor FROM controle_sync WHERE chave = %s", (chave,))
    linha = cursor.fetchone()
    return linha[0] if linha else None


def salvar_controle(conexao, chave, valor):
    cursor = conexao.cursor()
    cursor.execute("""
        INSERT INTO controle_sync (chave, valor) VALUES (%s, %s)
        ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
    """, (chave, valor))
    conexao.commit()
    return True


# ---------------------------------------------------------------------------
# Busca e gravacao das devolucoes
# ---------------------------------------------------------------------------

def erro_eh_sem_registros(retorno):
    """O Tiny responde com status 'Erro' tanto quando NAO ha registros no periodo
    (situacao normal) quanto quando algo realmente deu errado (bloqueio, queda...).
    Aqui separamos os dois casos, para nunca confundir 'falha' com 'nada novo'."""
    texto = (str(retorno.get("erros", "")) + " " + str(retorno.get("codigo_erro", ""))).lower()
    return ("nao retornou registros" in texto or "não retornou registros" in texto
            or str(retorno.get("codigo_erro", "")).strip() == "20")


def buscar_ids_notas_entrada(conexao):
    hoje = agora_brasil()

    ultima_data_str = obter_controle(conexao, "ultima_data_final_devolucoes")
    if ultima_data_str:
        ultima_data = datetime.strptime(ultima_data_str, "%d/%m/%Y")
        inicio = ultima_data - timedelta(days=MARGEM_DIAS_REPROCESSAMENTO)
        print(f"Sincronizacao incremental: buscando a partir de {inicio.strftime('%d/%m/%Y')}")
    else:
        inicio = hoje - timedelta(days=DIAS_PARA_BUSCAR)
        print(f"Primeira sincronizacao: buscando os ultimos {DIAS_PARA_BUSCAR} dias")

    ids_encontrados = []
    busca_ok = True      # vira False se a busca falhar de verdade (nao confundir com "sem notas")
    pagina = 1

    while True:
        payload = {
            "token": TOKEN,
            "formato": "JSON",
            "tipoNota": "E",
            "dataInicial": inicio.strftime("%d/%m/%Y"),
            "dataFinal": hoje.strftime("%d/%m/%Y"),
            "pagina": pagina,
        }

        dados = requisicao_resiliente(URL_PESQUISA, payload)
        retorno = dados.get("retorno", {})

        if retorno.get("status") == "Erro":
            if erro_eh_sem_registros(retorno):
                break   # normal: nao ha (mais) notas nesse periodo
            busca_ok = False
            print(f"ERRO ao buscar a pagina {pagina} da lista de notas: {retorno.get('erros') or retorno}")
            break

        notas = retorno.get("notas_fiscais", [])
        if not notas:
            break

        for n in notas:
            ids_encontrados.append(n["nota_fiscal"]["id"])

        total_paginas = int(retorno.get("numero_paginas", 1))
        print(f"Pagina {pagina} de {total_paginas} lida ({len(notas)} notas de entrada)")

        if pagina >= total_paginas:
            break

        pagina += 1
        time.sleep(INTERVALO_ENTRE_CHAMADAS)

    return ids_encontrados, busca_ok


def buscar_e_salvar_se_devolucao(conexao, nota_id):
    payload = {"token": TOKEN, "formato": "JSON", "id": nota_id}
    dados = requisicao_resiliente(URL_DETALHE, payload)
    retorno = dados.get("retorno", {})

    if retorno.get("status") == "Erro":
        print(f"  -> Erro ao buscar nota {nota_id}: {retorno.get('erros')}")
        return False, False

    nota = retorno.get("nota_fiscal", {})
    finalidade = nota.get("finalidade", "")
    agora = agora_brasil().strftime("%d/%m/%Y %H:%M:%S")

    cursor = conexao.cursor()
    eh_devolucao = finalidade in CODIGOS_FINALIDADE_DEVOLUCAO

    if eh_devolucao:
        cliente = nota.get("cliente", {}).get("nome", "")
        cursor.execute("""
            INSERT INTO devolucoes (
                id, numero, data_emissao, cliente, valor_produtos,
                valor_desconto, valor_nota, finalidade
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
                numero = EXCLUDED.numero,
                data_emissao = EXCLUDED.data_emissao,
                cliente = EXCLUDED.cliente,
                valor_produtos = EXCLUDED.valor_produtos,
                valor_desconto = EXCLUDED.valor_desconto,
                valor_nota = EXCLUDED.valor_nota,
                finalidade = EXCLUDED.finalidade
        """, (
            nota.get("id"), nota.get("numero"), nota.get("data_emissao"), cliente,
            float(nota.get("valor_produtos") or 0),
            float(nota.get("valor_desconto") or 0),
            float(nota.get("valor_nota") or 0),
            finalidade,
        ))

        cursor.execute("DELETE FROM itens_devolucao WHERE devolucao_id = %s", (nota.get("id"),))
        for item_wrapper in nota.get("itens", []):
            item = item_wrapper.get("item", {})
            cursor.execute("""
                INSERT INTO itens_devolucao (devolucao_id, sku, descricao, quantidade, valor_unitario, valor_total)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                nota.get("id"), item.get("codigo"), item.get("descricao"),
                float(item.get("quantidade") or 0),
                float(item.get("valor_unitario") or 0),
                float(item.get("valor_total") or 0),
            ))

    cursor.execute("""
        INSERT INTO notas_entrada_processadas (id, eh_devolucao, data_processamento)
        VALUES (%s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            eh_devolucao = EXCLUDED.eh_devolucao, data_processamento = EXCLUDED.data_processamento
    """, (nota_id, "Sim" if eh_devolucao else "Nao", agora))

    conexao.commit()
    return True, eh_devolucao


def registrar_execucao(conexao, nome, ok):
    """Anota quando esta sincronizacao rodou e quando foi a ultima que deu CERTO.
    E isso que permite o painel e o resumo diario avisarem se algo parou."""
    agora = agora_brasil().strftime("%Y-%m-%d %H:%M:%S")
    salvar_controle(conexao, f"ultima_execucao_{nome}", agora)
    if ok:
        salvar_controle(conexao, f"ultimo_sucesso_{nome}", agora)
    return True


def main():
    print("=== Namilay OS - Sincronizacao de Devolucoes com o Tiny ERP (-> nuvem) ===\n")

    if "COLE_SEU_TOKEN" in TOKEN:
        print("Voce ainda nao colocou o token do Tiny. Abra o config_banco.py e adicione a linha: TOKEN_TINY = 'seu_token'")
        return

    if not DATABASE_URL:
        print("Nao encontrei a connection string do banco (DATABASE_URL). No GitHub, confira o secret DATABASE_URL; no seu PC, o config_banco.py.")
        return

    conexao = conectar_com_tentativas()
    hoje = agora_brasil()

    print("Etapa 1/2: buscando notas de entrada...\n")
    ids, busca_ok = buscar_ids_notas_entrada(conexao)
    print(f"\nTotal de notas de entrada encontradas: {len(ids)}\n")

    print("Etapa 2/2: verificando quais sao devolucoes e salvando na nuvem...\n")
    devolucoes_novas = 0
    nao_devolucao = 0
    falhas = 0
    falhas_seguidas = 0

    for indice, nota_id in enumerate(ids, start=1):
        conexao, resultado = com_reconexao(conexao, buscar_e_salvar_se_devolucao, nota_id)
        if resultado is None:
            sucesso, eh_devolucao = False, False
        else:
            sucesso, eh_devolucao = resultado

        if sucesso:
            falhas_seguidas = 0
            if eh_devolucao:
                devolucoes_novas += 1
            else:
                nao_devolucao += 1
        else:
            falhas += 1
            falhas_seguidas += 1
            if falhas_seguidas >= FALHAS_SEGUIDAS_PARA_PARAR:
                print(f"\n{falhas_seguidas} notas seguidas falharam -- parando por seguranca.")
                break

        if indice % 10 == 0 or indice == len(ids):
            print(f"  Progresso: {indice}/{len(ids)} notas processadas "
                  f"({devolucoes_novas} devolucoes, {nao_devolucao} outras entradas)")

        time.sleep(INTERVALO_ENTRE_CHAMADAS)

    if falhas == 0 and busca_ok:
        conexao, _ = com_reconexao(
            conexao, salvar_controle, "ultima_data_final_devolucoes", hoje.strftime("%d/%m/%Y")
        )
        print(f"\nCONCLUIDO! {devolucoes_novas} devolucoes salvas/atualizadas na nuvem.")
    else:
        motivo = "" if busca_ok else " (a busca da lista de notas falhou)"
        print(f"\nTERMINOU COM PENDENCIAS{motivo}: {devolucoes_novas} devolucoes ok, {falhas} com falha.")
        print("O ponto de partida NAO foi avancado -- a proxima sincronizacao repete esses dias.")

    conexao, _ = com_reconexao(conexao, registrar_execucao, "devolucoes", falhas == 0 and busca_ok)

    try:
        conexao.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
