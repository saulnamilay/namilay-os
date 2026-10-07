"""
Namilay OS - Sincronizacao de Pedidos (Tiny ERP -> banco na nuvem/Supabase)
------------------------------------------------------------------------------
Grava direto no banco de dados na nuvem (Postgres/Supabase), em vez do
arquivo local namilay.db. Isso e o que faz o site publicado (em qualquer
lugar) sempre mostrar os dados atualizados.

Esta versao tolera falhas:
  - Se a internet cair, tenta de novo automaticamente (Tiny e banco de dados).
  - Se o Tiny bloquear por excesso de acessos, espera o bloqueio passar.
  - Se varios pedidos seguidos falharem, para de forma segura em vez de insistir.
  - So "avanca o ponto de partida" da proxima sincronizacao se tudo deu certo,
    para nunca deixar buracos nos dados.

COMO USAR:
  O token do Tiny fica no config_banco.py (TOKEN_TINY). Rode: python sincronizar_tiny_cloud.py
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
CHAMADAS_POR_MINUTO = 25   # parte da cota do Tiny; o resto fica para estoque/devolucoes
FALHAS_SEGUIDAS_PARA_PARAR = 3   # se tantos pedidos seguidos falharem, encerra com seguranca
# ================================================

URL_PESQUISA = "https://api.tiny.com.br/api2/pedidos.pesquisa.php"
URL_DETALHE = "https://api.tiny.com.br/api2/pedido.obter.php"
URL_NOTA_FISCAL = "https://api.tiny.com.br/api2/nota.fiscal.obter.php"

INTERVALO_ENTRE_CHAMADAS = 60 / CHAMADAS_POR_MINUTO


# ---------------------------------------------------------------------------
# Chamadas a API do Tiny (com tolerancia a falhas)
# ---------------------------------------------------------------------------

def requisicao_resiliente(url, payload, tentativas=4, espera_entre_tentativas=10):
    """Faz a chamada a API do Tiny com tolerancia a falhas:
    - se a internet cair, tenta de novo;
    - se o Tiny bloquear por excesso de acessos, espera o bloqueio passar e tenta de novo."""
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
    """As tabelas ja existem na nuvem (criadas pelo criar_tabelas_supabase.sql).
    Os parametros de keepalive ajudam a conexao a nao ser derrubada por inatividade."""
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
    """Executa operacao(conexao, *args). Se a conexao com o banco cair no meio,
    reconecta e tenta de novo (as operacoes aqui sao seguras de repetir).
    Retorna (conexao_atual, resultado). Se nao conseguir, resultado = None."""
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
# Busca e gravacao dos pedidos
# ---------------------------------------------------------------------------

def erro_eh_sem_registros(retorno):
    """O Tiny responde com status 'Erro' tanto quando NAO ha registros no periodo
    (situacao normal) quanto quando algo realmente deu errado (bloqueio, queda...).
    Aqui separamos os dois casos, para nunca confundir 'falha' com 'nada novo'."""
    texto = (str(retorno.get("erros", "")) + " " + str(retorno.get("codigo_erro", ""))).lower()
    return ("nao retornou registros" in texto or "não retornou registros" in texto
            or str(retorno.get("codigo_erro", "")).strip() == "20")


def buscar_ids_de_pedidos(conexao):
    hoje = agora_brasil()

    ultima_data_str = obter_controle(conexao, "ultima_data_final_pedidos")
    if ultima_data_str:
        ultima_data = datetime.strptime(ultima_data_str, "%d/%m/%Y")
        inicio = ultima_data - timedelta(days=MARGEM_DIAS_REPROCESSAMENTO)
        dias_janela = (hoje - inicio).days
        print(f"Sincronizacao incremental: buscando a partir de {inicio.strftime('%d/%m/%Y')} "
              f"(ultima sincronizacao completa foi ate {ultima_data_str}; janela de {dias_janela} dia(s))")
    else:
        inicio = hoje - timedelta(days=DIAS_PARA_BUSCAR)
        print(f"Primeira sincronizacao: buscando os ultimos {DIAS_PARA_BUSCAR} dias")

    ids_encontrados = []
    busca_ok = True      # vira False se a busca falhar de verdade (nao confundir com "sem pedidos")
    pagina = 1

    while True:
        payload = {
            "token": TOKEN,
            "formato": "JSON",
            "dataInicial": inicio.strftime("%d/%m/%Y"),
            "dataFinal": hoje.strftime("%d/%m/%Y"),
            "pagina": pagina,
        }

        dados = requisicao_resiliente(URL_PESQUISA, payload)
        retorno = dados.get("retorno", {})

        if retorno.get("status") == "Erro":
            if erro_eh_sem_registros(retorno):
                break   # normal: simplesmente nao ha (mais) pedidos nesse periodo
            busca_ok = False
            print(f"ERRO ao buscar a pagina {pagina} da lista de pedidos: {retorno.get('erros') or retorno}")
            break

        pedidos = retorno.get("pedidos", [])
        if not pedidos:
            break

        for p in pedidos:
            ids_encontrados.append(p["pedido"]["id"])

        total_paginas = int(retorno.get("numero_paginas", 1))
        print(f"Pagina {pagina} de {total_paginas} lida ({len(pedidos)} pedidos)")

        if pagina >= total_paginas:
            break

        pagina += 1
        time.sleep(INTERVALO_ENTRE_CHAMADAS)

    return ids_encontrados, busca_ok


def buscar_e_salvar_detalhe(conexao, pedido_id):
    """Busca o pedido (e a nota fiscal, se houver) e grava tudo numa unica transacao.
    E seguro repetir: usa UPSERT no pedido e recria os itens dele."""
    payload = {"token": TOKEN, "formato": "JSON", "id": pedido_id}
    dados = requisicao_resiliente(URL_DETALHE, payload)
    retorno = dados.get("retorno", {})

    if retorno.get("status") == "Erro":
        print(f"  -> Erro ao buscar pedido {pedido_id}: {retorno.get('erros')}")
        return False

    pedido = retorno.get("pedido", {})
    ecommerce = pedido.get("ecommerce") or {}

    itens_para_salvar = pedido.get("itens", [])
    fonte_itens = "pedido"

    id_nota_fiscal = pedido.get("id_nota_fiscal")
    if id_nota_fiscal:
        time.sleep(INTERVALO_ENTRE_CHAMADAS)
        payload_nf = {"token": TOKEN, "formato": "JSON", "id": id_nota_fiscal}
        dados_nf = requisicao_resiliente(URL_NOTA_FISCAL, payload_nf)
        retorno_nf = dados_nf.get("retorno", {})
        if retorno_nf.get("status") != "Erro":
            nota = retorno_nf.get("nota_fiscal", {})
            itens_nf = nota.get("itens", [])
            if itens_nf:
                # A nota fiscal ja vem com kits "explodidos" nos SKUs reais -- mais precisa
                itens_para_salvar = [
                    {"item": {
                        "codigo": iw.get("item", {}).get("codigo"),
                        "descricao": iw.get("item", {}).get("descricao"),
                        "quantidade": iw.get("item", {}).get("quantidade"),
                        "valor_unitario": iw.get("item", {}).get("valor_unitario"),
                    }}
                    for iw in itens_nf
                ]
                fonte_itens = "nota_fiscal"

    # Toda a gravacao acontece junto e so vale depois do commit final:
    # se a conexao cair no meio, nada fica pela metade.
    cursor = conexao.cursor()
    cursor.execute("""
        INSERT INTO pedidos (
            id, numero, numero_ecommerce, marketplace, data_pedido,
            data_faturamento, data_envio, data_entrega, situacao,
            valor_frete, valor_desconto, total_produtos, total_pedido
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            numero = EXCLUDED.numero,
            numero_ecommerce = EXCLUDED.numero_ecommerce,
            marketplace = EXCLUDED.marketplace,
            data_pedido = EXCLUDED.data_pedido,
            data_faturamento = EXCLUDED.data_faturamento,
            data_envio = EXCLUDED.data_envio,
            data_entrega = EXCLUDED.data_entrega,
            situacao = EXCLUDED.situacao,
            valor_frete = EXCLUDED.valor_frete,
            valor_desconto = EXCLUDED.valor_desconto,
            total_produtos = EXCLUDED.total_produtos,
            total_pedido = EXCLUDED.total_pedido
    """, (
        pedido.get("id"),
        pedido.get("numero"),
        pedido.get("numero_ecommerce"),
        ecommerce.get("nomeEcommerce", "Desconhecido"),
        pedido.get("data_pedido"),
        pedido.get("data_faturamento"),
        pedido.get("data_envio"),
        pedido.get("data_entrega"),
        pedido.get("situacao"),
        float(pedido.get("valor_frete") or 0),
        float(pedido.get("valor_desconto") or 0),
        float(pedido.get("total_produtos") or 0),
        float(pedido.get("total_pedido") or 0),
    ))

    cursor.execute("DELETE FROM itens_pedido WHERE pedido_id = %s", (pedido.get("id"),))

    for item_wrapper in itens_para_salvar:
        item = item_wrapper.get("item", {})
        cursor.execute("""
            INSERT INTO itens_pedido (pedido_id, sku, descricao, quantidade, valor_unitario, fonte_itens)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (
            pedido.get("id"),
            item.get("codigo"),
            item.get("descricao"),
            float(item.get("quantidade") or 0),
            float(item.get("valor_unitario") or 0),
            fonte_itens,
        ))

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
    print("=== Namilay OS - Sincronizacao com o Tiny ERP (-> nuvem) ===\n")

    if "COLE_SEU_TOKEN" in TOKEN:
        print("Voce ainda nao colocou o token do Tiny. Abra o config_banco.py e adicione a linha: TOKEN_TINY = 'seu_token'")
        return

    if not DATABASE_URL:
        print("Nao encontrei a connection string do banco (DATABASE_URL). No GitHub, confira o secret DATABASE_URL; no seu PC, o config_banco.py.")
        return

    conexao = conectar_com_tentativas()
    hoje = agora_brasil()

    print("Etapa 1/2: buscando lista de pedidos...\n")
    ids, busca_ok = buscar_ids_de_pedidos(conexao)
    print(f"\nTotal de pedidos encontrados: {len(ids)}\n")

    print("Etapa 2/2: buscando o detalhe de cada pedido e salvando/atualizando na nuvem...\n")
    processados = 0
    falhas = 0
    falhas_seguidas = 0
    interrompido = False

    for indice, pedido_id in enumerate(ids, start=1):
        conexao, sucesso = com_reconexao(conexao, buscar_e_salvar_detalhe, pedido_id)
        if sucesso:
            processados += 1
            falhas_seguidas = 0
        else:
            falhas += 1
            falhas_seguidas += 1
            if falhas_seguidas >= FALHAS_SEGUIDAS_PARA_PARAR:
                print(f"\n{falhas_seguidas} pedidos seguidos falharam -- parando por seguranca.")
                print("Causas comuns: outra sincronizacao rodando ao mesmo tempo (o Tiny limita as")
                print("chamadas por minuto da conta inteira) ou internet instavel. Tente de novo depois.")
                interrompido = True
                break

        if indice % 10 == 0 or indice == len(ids):
            print(f"  Progresso: {indice}/{len(ids)} pedidos processados")

        time.sleep(INTERVALO_ENTRE_CHAMADAS)

    if falhas == 0 and not interrompido and busca_ok:
        conexao, _ = com_reconexao(
            conexao, salvar_controle, "ultima_data_final_pedidos", hoje.strftime("%d/%m/%Y")
        )
        print(f"\nCONCLUIDO! {processados} pedidos salvos/atualizados na nuvem.")
    else:
        motivo = "" if busca_ok else " (a busca da lista de pedidos falhou)"
        print(f"\nTERMINOU COM PENDENCIAS{motivo}: {processados} pedidos salvos, {falhas} com falha.")
        print("O ponto de partida NAO foi avancado -- a proxima sincronizacao vai tentar")
        print("de novo esses mesmos dias (e seguro repetir, nada e duplicado).")

    conexao, _ = com_reconexao(conexao, registrar_execucao, "vendas", falhas == 0 and not interrompido and busca_ok)

    try:
        conexao.close()
    except Exception:
        pass


if __name__ == "__main__":
    main()
