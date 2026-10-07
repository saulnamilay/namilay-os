"""
Namilay OS - Sincronizacao de Estoque (Tiny ERP -> banco na nuvem/Supabase)
------------------------------------------------------------------------------
Grava direto no banco de dados na nuvem (Postgres/Supabase).

IMPORTANTE (correcao em relacao a versao antiga): esta versao SEMPRE
atualiza o estoque de todos os produtos ativos a cada execucao. A versao
antiga (arquivo local) pulava produtos ja processados para sempre depois
da primeira vez -- o que significava que o estoque deles nunca mais era
atualizado. Estoque muda o tempo todo, entao isso precisa ser sempre
reconferido.

COMO USAR:
  O token do Tiny fica no config_banco.py (TOKEN_TINY). Rode: python sincronizar_estoque_cloud.py
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
CHAMADAS_POR_MINUTO = 20   # parte da cota do Tiny, para nao brigar com a sincronizacao de vendas
FALHAS_SEGUIDAS_PARA_PARAR = 3
# ================================================

URL_PRODUTOS = "https://api.tiny.com.br/api2/produtos.pesquisa.php"
URL_ESTOQUE = "https://api.tiny.com.br/api2/produto.obter.estoque.php"

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
# Busca e gravacao do estoque
# ---------------------------------------------------------------------------

def erro_eh_sem_registros(retorno):
    """O Tiny usa status 'Erro' tanto para 'nada encontrado' (normal) quanto para falhas reais."""
    texto = (str(retorno.get("erros", "")) + " " + str(retorno.get("codigo_erro", ""))).lower()
    return ("nao retornou registros" in texto or "não retornou registros" in texto
            or str(retorno.get("codigo_erro", "")).strip() == "20")


def buscar_todos_produtos():
    """Percorre todas as paginas de produtos.pesquisa.php (busca vazia = todos ativos)."""
    produtos_encontrados = []
    busca_ok = True   # vira False se a busca falhar de verdade
    pagina = 1

    while True:
        payload = {
            "token": TOKEN,
            "formato": "JSON",
            "pesquisa": "",
            "situacao": "A",
            "pagina": pagina,
        }

        dados = requisicao_resiliente(URL_PRODUTOS, payload)
        retorno = dados.get("retorno", {})

        if retorno.get("status") == "Erro":
            if erro_eh_sem_registros(retorno):
                break
            busca_ok = False
            print(f"ERRO ao buscar a pagina {pagina} da lista de produtos: {retorno.get('erros') or retorno}")
            break

        produtos = retorno.get("produtos", [])
        if not produtos:
            break

        for p in produtos:
            produtos_encontrados.append(p["produto"])

        total_paginas = int(retorno.get("numero_paginas", 1))
        print(f"Pagina {pagina} de {total_paginas} lida ({len(produtos)} produtos)")

        if pagina >= total_paginas:
            break

        pagina += 1
        time.sleep(INTERVALO_ENTRE_CHAMADAS)

    return produtos_encontrados, busca_ok


def buscar_e_salvar_estoque(conexao, produto_id, sku, descricao):
    """Busca o estoque atual do produto e SUBSTITUI (nao acumula) os registros dele.
    Sempre roda, mesmo que o produto ja tenha sido processado antes -- estoque muda."""
    payload = {"token": TOKEN, "formato": "JSON", "id": produto_id}
    dados = requisicao_resiliente(URL_ESTOQUE, payload)
    retorno = dados.get("retorno", {})

    if retorno.get("status") == "Erro":
        print(f"  -> Erro ao buscar estoque do produto {sku}: {retorno.get('erros')}")
        return False

    produto = retorno.get("produto", {})
    depositos = produto.get("depositos", [])
    agora = agora_brasil().strftime("%d/%m/%Y %H:%M:%S")

    cursor = conexao.cursor()

    # Remove o snapshot anterior desse SKU e grava o atual -- assim o estoque
    # sempre reflete a ultima consulta, sem acumular linhas antigas.
    cursor.execute("DELETE FROM estoque_snapshot WHERE sku = %s", (sku,))

    if not depositos:
        cursor.execute("""
            INSERT INTO estoque_snapshot (sku, descricao, deposito, saldo, conta_no_total, data_referencia)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (sku, descricao, "Geral", float(produto.get("saldo") or 0), "N/D", agora))
    else:
        for dep_wrapper in depositos:
            dep = dep_wrapper.get("deposito", {})
            cursor.execute("""
                INSERT INTO estoque_snapshot (sku, descricao, deposito, saldo, conta_no_total, data_referencia)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                sku, descricao, dep.get("nome"), float(dep.get("saldo") or 0),
                "Sim" if dep.get("desconsiderar") == "N" else "Nao", agora,
            ))

    cursor.execute("""
        INSERT INTO produtos_processados (id_tiny, sku, data_processamento)
        VALUES (%s, %s, %s)
        ON CONFLICT (id_tiny) DO UPDATE SET data_processamento = EXCLUDED.data_processamento
    """, (produto_id, sku, agora))

    conexao.commit()
    return True


def salvar_controle(conexao, chave, valor):
    cursor = conexao.cursor()
    cursor.execute("""
        INSERT INTO controle_sync (chave, valor) VALUES (%s, %s)
        ON CONFLICT (chave) DO UPDATE SET valor = EXCLUDED.valor
    """, (chave, valor))
    conexao.commit()
    return True


def registrar_execucao(conexao, nome, ok):
    """Anota quando esta sincronizacao rodou e quando foi a ultima que deu CERTO.
    E isso que permite o painel e o resumo diario avisarem se algo parou."""
    agora = agora_brasil().strftime("%Y-%m-%d %H:%M:%S")
    salvar_controle(conexao, f"ultima_execucao_{nome}", agora)
    if ok:
        salvar_controle(conexao, f"ultimo_sucesso_{nome}", agora)
    return True


def main():
    print("=== Namilay OS - Sincronizacao de Estoque com o Tiny ERP (-> nuvem) ===\n")

    if "COLE_SEU_TOKEN" in TOKEN:
        print("Voce ainda nao colocou o token do Tiny. Abra o config_banco.py e adicione a linha: TOKEN_TINY = 'seu_token'")
        return

    if not DATABASE_URL:
        print("Nao encontrei a connection string do banco (DATABASE_URL). No GitHub, confira o secret DATABASE_URL; no seu PC, o config_banco.py.")
        return

    conexao = conectar_com_tentativas()

    print("Etapa 1/2: buscando lista de todos os produtos ativos...\n")
    produtos, produtos_ok = buscar_todos_produtos()
    print(f"\nTotal de produtos encontrados: {len(produtos)}\n")

    print("Etapa 2/2: buscando o estoque de cada produto e atualizando na nuvem...\n")
    atualizados = 0
    falhas = 0
    falhas_seguidas = 0

    for indice, produto in enumerate(produtos, start=1):
        produto_id = produto.get("id")
        sku = produto.get("codigo")
        descricao = produto.get("nome")

        conexao, sucesso = com_reconexao(conexao, buscar_e_salvar_estoque, produto_id, sku, descricao)
        if sucesso:
            atualizados += 1
            falhas_seguidas = 0
        else:
            falhas += 1
            falhas_seguidas += 1
            if falhas_seguidas >= FALHAS_SEGUIDAS_PARA_PARAR:
                print(f"\n{falhas_seguidas} produtos seguidos falharam -- parando por seguranca.")
                break

        if indice % 10 == 0 or indice == len(produtos):
            print(f"  Progresso: {indice}/{len(produtos)} produtos processados")

        time.sleep(INTERVALO_ENTRE_CHAMADAS)

    if falhas == 0 and produtos_ok:
        print(f"\nCONCLUIDO! {atualizados} produtos com estoque atualizado na nuvem.")
    else:
        motivo = "" if produtos_ok else " (a busca da lista de produtos falhou)"
        print(f"\nTERMINOU COM PENDENCIAS{motivo}: {atualizados} produtos atualizados, {falhas} com falha.")

    conexao, _ = com_reconexao(conexao, registrar_execucao, "estoque", falhas == 0 and produtos_ok)

    try:
        conexao.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
