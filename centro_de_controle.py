"""
Namilay OS - Centro de Controle (versao web local, com Streamlit)
----------------------------------------------------------------------
Isso e a primeira tela de verdade do Namilay OS: um "site" que roda no
seu computador, le os bancos de dados que ja sincronizamos, e mostra
tudo de forma visual e interativa.

COMO USAR:
  1. Instale o streamlit (se ainda nao instalou):
       pip install streamlit
  2. Deixe este arquivo na MESMA pasta onde estao o namilay.db e o
     namilay_estoque.db (a pasta Downloads).
  3. Rode (repare que e "streamlit run", nao "python"):
       streamlit run centro_de_controle.py
  4. Vai abrir uma aba no seu navegador automaticamente. Se nao abrir,
     o terminal mostra um endereco tipo http://localhost:8501 para
     voce colar no navegador.
  5. Para fechar, volte ao terminal e aperte Ctrl+C.

Se algum dos dois bancos de dados ainda nao existir (por exemplo, se a
sincronizacao de pedidos ainda nao terminou), a tela correspondente vai
mostrar um aviso em vez de dar erro.
"""

import os
import sqlite3
import calendar
import math
from datetime import datetime
import pandas as pd
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    AUTOREFRESH_DISPONIVEL = True
except ImportError:
    AUTOREFRESH_DISPONIVEL = False

BANCO_VENDAS = "namilay.db"
BANCO_ESTOQUE = "namilay_estoque.db"
BANCO_DEVOLUCOES = "namilay_devolucoes.db"
BANCO_ENVIOS = "namilay_envios.db"
SITUACOES_EXCLUIDAS = ["Cancelado"]

MESES_PT = {
    1: "Janeiro", 2: "Fevereiro", 3: "Marco", 4: "Abril", 5: "Maio", 6: "Junho",
    7: "Julho", 8: "Agosto", 9: "Setembro", 10: "Outubro", 11: "Novembro", 12: "Dezembro",
}


def formatar_moeda(valor):
    """Formata um numero como moeda brasileira: R$ 10.500,12"""
    if valor is None or pd.isna(valor):
        valor = 0
    texto = f"{valor:,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def formatar_percentual(valor):
    if valor is None or pd.isna(valor):
        valor = 0
    return f"{valor:.1f}%".replace(".", ",")


def formatar_colunas_moeda(df, colunas):
    """Retorna uma copia do dataframe com as colunas indicadas formatadas como moeda BR (string).
    Use isso SOMENTE depois de ordenar o dataframe -- a ordenacao deve ser feita antes,
    pois apos formatar a coluna vira texto."""
    df_formatado = df.copy()
    for col in colunas:
        df_formatado[col] = df_formatado[col].apply(formatar_moeda)
    return df_formatado


def titulo_coluna(col):
    """Deixa o nome da coluna com a primeira letra maiuscula (ex: 'marketplace' -> 'Marketplace').
    Nomes que ja tem letra maiuscula (ex: nomes de deposito como 'Mercado Livre Full') ficam como estao."""
    if col.upper() == "SKU":
        return "SKU"
    if any(c.isupper() for c in col):
        return col
    return col.replace("_", " ").strip().title()


def capitalizar_colunas(df):
    """Retorna uma copia do dataframe com todos os cabecalhos de coluna capitalizados."""
    return df.rename(columns={c: titulo_coluna(c) for c in df.columns})

st.set_page_config(page_title="Namilay OS", layout="wide", page_icon="📦")

COR_FUNDO = "#002342"
COR_FUNDO_CARD = "#0B3A64"
COR_TEAL = "#4FC9A0"
COR_TEXTO = "#F2F5F7"
COR_TEXTO_SECUNDARIO = "#9FB4C7"

st.markdown(f"""
<style>
    .stApp {{ background-color: {COR_FUNDO}; }}
    h1, h2, h3 {{ color: {COR_TEXTO}; font-weight: 700; }}
    p, span, label, div {{ color: {COR_TEXTO}; }}
    div[data-testid="stMetric"] {{
        background-color: {COR_FUNDO_CARD};
        border: 1px solid rgba(255,255,255,0.08);
        border-left: 4px solid {COR_TEAL};
        border-radius: 10px;
        padding: 1rem 1.2rem;
    }}
    div[data-testid="stMetricLabel"] {{ color: {COR_TEXTO_SECUNDARIO} !important; }}
    div[data-testid="stMetricValue"] {{ color: {COR_TEXTO} !important; }}
    .stTabs [data-baseweb="tab"] {{ font-weight: 600; color: {COR_TEXTO_SECUNDARIO}; }}
    .stTabs [aria-selected="true"] {{ color: {COR_TEAL} !important; }}
    .stTabs [data-baseweb="tab-highlight"] {{ background-color: {COR_TEAL} !important; }}
    .stButton button {{ background-color: {COR_TEAL}; color: {COR_FUNDO}; border: none; font-weight: 600; }}
    div[data-testid="stExpander"] {{ background-color: {COR_FUNDO_CARD}; border-radius: 10px; }}
</style>
""", unsafe_allow_html=True)


def cor_percentual_devolucao(valor):
    """Verde = boa noticia (devolucao baixa), vermelho = ruim (devolucao alta). So a letra, sem fundo."""
    if pd.isna(valor):
        return ""
    if valor >= 15:
        return "color: #F87171; font-weight: 600;"
    elif valor >= 5:
        return "color: #FBBF24; font-weight: 600;"
    else:
        return "color: #34D399; font-weight: 600;"


def cartao_metrica(coluna, label, valor_texto, cor_valor=None):
    """Metrica customizada em HTML, para permitir cor no numero (verde/vermelho)."""
    cor = cor_valor or COR_TEXTO
    coluna.markdown(f"""
        <div style="background:{COR_FUNDO_CARD};border:1px solid rgba(255,255,255,0.08);
                    border-left:4px solid {COR_TEAL};border-radius:10px;padding:1rem 1.2rem;">
            <div style="font-size:0.85rem;color:{COR_TEXTO_SECUNDARIO};">{label}</div>
            <div style="font-size:1.6rem;font-weight:700;color:{cor};">{valor_texto}</div>
        </div>
    """, unsafe_allow_html=True)


# ------------------------- Funcoes de dados -------------------------

@st.cache_data(ttl=30)
def carregar_vendas():
    conexao = sqlite3.connect(BANCO_VENDAS)
    pedidos = pd.read_sql_query("SELECT * FROM pedidos", conexao)
    itens = pd.read_sql_query("SELECT * FROM itens_pedido", conexao)
    conexao.close()
    return pedidos, itens


@st.cache_data(ttl=30)
def carregar_estoque():
    conexao = sqlite3.connect(BANCO_ESTOQUE)
    dados = pd.read_sql_query("SELECT * FROM estoque_snapshot", conexao)
    conexao.close()
    return dados


@st.cache_data(ttl=30)
def carregar_devolucoes():
    conexao = sqlite3.connect(BANCO_DEVOLUCOES)
    devolucoes = pd.read_sql_query("SELECT * FROM devolucoes", conexao)
    itens_devolucao = pd.read_sql_query("SELECT * FROM itens_devolucao", conexao)
    conexao.close()
    return devolucoes, itens_devolucao


def preparar_banco_envios():
    """Diferente das outras, essa NAO usa cache -- porque a gente escreve nela."""
    conexao = sqlite3.connect(BANCO_ENVIOS)
    cursor = conexao.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS envios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_prevista TEXT,
            destino TEXT,
            status TEXT,
            observacao TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS envio_itens (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            envio_id INTEGER,
            sku TEXT,
            descricao TEXT,
            quantidade REAL
        )
    """)
    # Migracao segura: adiciona a coluna se ainda nao existir (nao apaga nada)
    cursor.execute("PRAGMA table_info(envios)")
    colunas_existentes = [linha[1] for linha in cursor.fetchall()]
    if "transferencia_estoque" not in colunas_existentes:
        cursor.execute("ALTER TABLE envios ADD COLUMN transferencia_estoque TEXT DEFAULT 'Nao'")
    conexao.commit()
    return conexao


# ------------------------- Cabecalho -------------------------

LOGO_PATH = "logo_namilay.png"
if os.path.exists(LOGO_PATH):
    st.image(LOGO_PATH, width=220)
else:
    st.markdown(f"""
        <div style="font-size:2rem;font-weight:800;color:{COR_TEXTO};">Namilay OS</div>
        <div style="font-size:0.8rem;color:#FBBF24;">
            (logo_namilay.png nao encontrado nesta pasta -- usando texto como substituto)
        </div>
    """, unsafe_allow_html=True)
st.caption("Centro de controle da operacao - versao inicial (MVP)")

INTERVALO_AUTOREFRESH_SEGUNDOS = 30
if AUTOREFRESH_DISPONIVEL:
    st_autorefresh(interval=INTERVALO_AUTOREFRESH_SEGUNDOS * 1000, key="autorefresh_namilay")
    st.caption(f"🔄 Atualizando sozinho a cada {INTERVALO_AUTOREFRESH_SEGUNDOS}s — ultima checagem: {datetime.now().strftime('%H:%M:%S')}")
else:
    st.caption(
        "Para essa tela se atualizar sozinha, instale: pip install streamlit-autorefresh "
        "(por enquanto, aperte F5 para atualizar manualmente)."
    )

aba_vendas, aba_estoque, aba_devolucao, aba_calendario, aba_inteligencia = st.tabs(
    ["Vendas", "Estoque", "Devolucao", "Calendario", "Inteligencia"]
)


# ------------------------- Aba de Vendas -------------------------

with aba_vendas:
    if not os.path.exists(BANCO_VENDAS):
        st.warning(
            f"O arquivo {BANCO_VENDAS} ainda nao existe nesta pasta. "
            "Rode o sincronizar_tiny.py primeiro."
        )
    else:
        pedidos, itens = carregar_vendas()
        pedidos_validos = pedidos[~pedidos["situacao"].isin(SITUACOES_EXCLUIDAS)].copy()
        pedidos_validos["_data"] = pd.to_datetime(
            pedidos_validos["data_pedido"], format="%d/%m/%Y", errors="coerce"
        )

        # ---- Filtros ----
        with st.expander("Filtros", expanded=True):
            data_min = pedidos_validos["_data"].min()
            data_max = pedidos_validos["_data"].max()

            col_btn1, col_btn2, col_btn3 = st.columns(3)
            if col_btn1.button("Ultimos 7 dias"):
                st.session_state["periodo_vendas"] = (
                    max(data_min, data_max - pd.Timedelta(days=6)), data_max
                )
            if col_btn2.button("Ultimos 30 dias"):
                st.session_state["periodo_vendas"] = (
                    max(data_min, data_max - pd.Timedelta(days=29)), data_max
                )
            if col_btn3.button("Tudo"):
                st.session_state["periodo_vendas"] = (data_min, data_max)

            if "periodo_vendas" not in st.session_state:
                st.session_state["periodo_vendas"] = (data_min, data_max)

            col_f1, col_f2 = st.columns([1, 2])
            periodo = col_f1.date_input(
                "Periodo",
                min_value=data_min,
                max_value=data_max,
                key="periodo_vendas",
            )

            marketplaces_disponiveis = sorted(pedidos_validos["marketplace"].dropna().unique())
            marketplaces_selecionados = col_f2.multiselect(
                "Marketplaces",
                options=marketplaces_disponiveis,
                default=marketplaces_disponiveis,
            )

        if isinstance(periodo, tuple) and len(periodo) == 2:
            inicio, fim = periodo
            pedidos_filtrados = pedidos_validos[
                (pedidos_validos["_data"] >= pd.Timestamp(inicio))
                & (pedidos_validos["_data"] <= pd.Timestamp(fim))
            ]
        else:
            pedidos_filtrados = pedidos_validos

        if marketplaces_selecionados:
            pedidos_filtrados = pedidos_filtrados[
                pedidos_filtrados["marketplace"].isin(marketplaces_selecionados)
            ]

        # ---- Periodo anterior (mesma duracao, imediatamente antes) para comparacao ----
        pedidos_periodo_anterior = pd.DataFrame()
        if isinstance(periodo, tuple) and len(periodo) == 2:
            duracao_dias = (pd.Timestamp(fim) - pd.Timestamp(inicio)).days + 1
            inicio_anterior = pd.Timestamp(inicio) - pd.Timedelta(days=duracao_dias)
            fim_anterior = pd.Timestamp(inicio) - pd.Timedelta(days=1)
            pedidos_periodo_anterior = pedidos_validos[
                (pedidos_validos["_data"] >= inicio_anterior) & (pedidos_validos["_data"] <= fim_anterior)
            ]
            if marketplaces_selecionados:
                pedidos_periodo_anterior = pedidos_periodo_anterior[
                    pedidos_periodo_anterior["marketplace"].isin(marketplaces_selecionados)
                ]

        def calcular_delta_pct(valor_atual, valor_anterior):
            if not valor_anterior:
                return None
            return f"{(valor_atual - valor_anterior) / valor_anterior * 100:+.1f}%"

        ids_anterior = set(pedidos_periodo_anterior["id"]) if not pedidos_periodo_anterior.empty else set()
        unidades_anterior = itens[itens["pedido_id"].isin(ids_anterior)]["quantidade"].sum()

        # ---- KPIs ----
        st.caption("A variacao (seta colorida) compara com o periodo imediatamente anterior, de mesma duracao.")
        col1, col2, col3, col4 = st.columns(4)

        fat_atual = pedidos_filtrados['total_pedido'].sum()
        fat_anterior = pedidos_periodo_anterior['total_pedido'].sum() if not pedidos_periodo_anterior.empty else 0
        col1.metric("Faturamento total", formatar_moeda(fat_atual), delta=calcular_delta_pct(fat_atual, fat_anterior))

        col2.metric(
            "Pedidos validos", f"{len(pedidos_filtrados)}",
            delta=calcular_delta_pct(len(pedidos_filtrados), len(pedidos_periodo_anterior)),
        )

        ticket_medio = pedidos_filtrados["total_pedido"].mean() if len(pedidos_filtrados) > 0 else 0
        ticket_anterior = pedidos_periodo_anterior["total_pedido"].mean() if len(pedidos_periodo_anterior) > 0 else 0
        col3.metric("Ticket medio", formatar_moeda(ticket_medio), delta=calcular_delta_pct(ticket_medio, ticket_anterior))

        ids_filtrados = set(pedidos_filtrados["id"])
        unidades_vendidas = itens[itens["pedido_id"].isin(ids_filtrados)]["quantidade"].sum()
        col4.metric(
            "Unidades vendidas", f"{int(unidades_vendidas)}",
            delta=calcular_delta_pct(unidades_vendidas, unidades_anterior),
        )

        # ---- Pedidos cancelados (mesmo periodo/marketplace, com comparacao) ----
        cancelados = pedidos[pedidos["situacao"].isin(SITUACOES_EXCLUIDAS)].copy()
        cancelados["_data"] = pd.to_datetime(cancelados["data_pedido"], format="%d/%m/%Y", errors="coerce")

        cancelados_anterior = pd.DataFrame()
        if isinstance(periodo, tuple) and len(periodo) == 2:
            cancelados_filtro_periodo = cancelados[
                (cancelados["_data"] >= pd.Timestamp(periodo[0]))
                & (cancelados["_data"] <= pd.Timestamp(periodo[1]))
            ]
            cancelados_anterior = cancelados[
                (cancelados["_data"] >= inicio_anterior) & (cancelados["_data"] <= fim_anterior)
            ]
        else:
            cancelados_filtro_periodo = cancelados

        if marketplaces_selecionados:
            cancelados_filtro_periodo = cancelados_filtro_periodo[
                cancelados_filtro_periodo["marketplace"].isin(marketplaces_selecionados)
            ]
            if not cancelados_anterior.empty:
                cancelados_anterior = cancelados_anterior[
                    cancelados_anterior["marketplace"].isin(marketplaces_selecionados)
                ]
        cancelados = cancelados_filtro_periodo

        col_c1, col_c2 = st.columns(2)
        col_c1.metric(
            "Pedidos cancelados", f"{len(cancelados)}",
            delta=calcular_delta_pct(len(cancelados), len(cancelados_anterior)),
            delta_color="inverse",
        )
        valor_cancelado_atual = cancelados["total_pedido"].sum()
        valor_cancelado_anterior = cancelados_anterior["total_pedido"].sum() if not cancelados_anterior.empty else 0
        col_c2.metric(
            "Valor cancelado", formatar_moeda(valor_cancelado_atual),
            delta=calcular_delta_pct(valor_cancelado_atual, valor_cancelado_anterior),
            delta_color="inverse",
        )

        # ---- Devolucoes (do banco separado) ----
        if os.path.exists(BANCO_DEVOLUCOES):
            devolucoes, _ = carregar_devolucoes()
            col_d1, col_d2 = st.columns(2)
            col_d1.metric("Devolucoes (periodo total sincronizado)", f"{len(devolucoes)}")
            col_d2.metric("Valor devolvido", formatar_moeda(devolucoes["valor_nota"].sum()))
            st.caption(
                "Devolucoes vem de uma sincronizacao separada (notas fiscais de entrada) e "
                "cobrem o periodo sincronizado por ela, que pode ser diferente do filtro de data acima. "
                "Veja a aba Devolucao para o detalhamento."
            )
        else:
            st.caption(
                f"Para ver dados de devolucao aqui, rode o sincronizar_devolucoes_tiny.py "
                f"(vai gerar o arquivo {BANCO_DEVOLUCOES})."
            )

        # ---- Faturamento por marketplace ----
        st.subheader("Faturamento por marketplace")
        por_marketplace = (
            pedidos_filtrados.groupby("marketplace")
            .agg(pedidos=("id", "count"), faturamento=("total_pedido", "sum"))
            .reset_index()
            .sort_values("faturamento", ascending=False)
        )
        por_marketplace["ticket_medio"] = por_marketplace["faturamento"] / por_marketplace["pedidos"]
        total_pedidos_geral = por_marketplace["pedidos"].sum()
        por_marketplace["% dos pedidos"] = (
            por_marketplace["pedidos"] / total_pedidos_geral * 100
        ).apply(formatar_percentual)

        col_grafico, col_tabela = st.columns([2, 1])
        col_grafico.bar_chart(por_marketplace.set_index("marketplace")["faturamento"])

        tabela_mkt_exibicao = formatar_colunas_moeda(por_marketplace, ["faturamento", "ticket_medio"])
        col_tabela.dataframe(
            capitalizar_colunas(
                tabela_mkt_exibicao[["marketplace", "pedidos", "% dos pedidos", "faturamento", "ticket_medio"]]
            ),
            width='stretch', hide_index=True,
        )

        # ---- Evolucao diaria (linha por marketplace) ----
        st.subheader("Evolucao diaria de faturamento, por marketplace")
        agrupamento = st.radio("Agrupar por", ["Dia", "Semana", "Mes"], horizontal=True)

        base_evolucao = pedidos_filtrados.copy()
        if agrupamento == "Semana":
            base_evolucao["_periodo"] = base_evolucao["_data"].dt.to_period("W").apply(lambda p: p.start_time)
        elif agrupamento == "Mes":
            base_evolucao["_periodo"] = base_evolucao["_data"].dt.to_period("M").apply(lambda p: p.start_time)
        else:
            base_evolucao["_periodo"] = base_evolucao["_data"]

        evolucao_mkt = base_evolucao.pivot_table(
            index="_periodo", columns="marketplace", values="total_pedido", aggfunc="sum", fill_value=0
        ).sort_index()
        st.line_chart(evolucao_mkt)

        # ---- Projecao para o mes que vem ----
        st.subheader("Projecao para o mes que vem")
        st.caption(
            "Estimativa simples: media de faturamento diario dos ultimos 30 dias com dados, "
            "multiplicada pelos dias do proximo mes. Nao considera sazonalidade ainda -- "
            "isso entra no modulo de Inteligencia mais pra frente."
        )

        data_max_geral = pedidos_validos["_data"].max()
        ultimos_30_dias = pedidos_validos[
            pedidos_validos["_data"] >= (data_max_geral - pd.Timedelta(days=29))
        ]

        if len(ultimos_30_dias) == 0 or pd.isna(data_max_geral):
            st.info("Nao ha dados suficientes nos ultimos 30 dias para calcular uma projecao.")
        else:
            media_diaria_geral = (
                ultimos_30_dias.groupby(ultimos_30_dias["_data"].dt.date)["total_pedido"]
                .sum()
                .mean()
            )

            hoje_real = datetime.now()
            if hoje_real.month == 12:
                mes_seguinte, ano_seguinte = 1, hoje_real.year + 1
            else:
                mes_seguinte, ano_seguinte = hoje_real.month + 1, hoje_real.year
            dias_mes_seguinte = calendar.monthrange(ano_seguinte, mes_seguinte)[1]
            projecao_total = media_diaria_geral * dias_mes_seguinte

            col_p1, col_p2, col_p3 = st.columns(3)
            col_p1.metric("Media diaria (ultimos 30 dias)", formatar_moeda(media_diaria_geral))
            col_p2.metric(f"Dias em {MESES_PT[mes_seguinte]}", f"{dias_mes_seguinte}")
            col_p3.metric(f"Projecao para {MESES_PT[mes_seguinte]}", formatar_moeda(projecao_total))

            with st.expander("Ver projecao detalhada por marketplace"):
                media_por_mkt = (
                    ultimos_30_dias.groupby(["marketplace", ultimos_30_dias["_data"].dt.date])["total_pedido"]
                    .sum()
                    .reset_index()
                    .groupby("marketplace")["total_pedido"]
                    .mean()
                    .reset_index()
                    .rename(columns={"total_pedido": "media_diaria"})
                )
                media_por_mkt["projecao_mes_seguinte"] = media_por_mkt["media_diaria"] * dias_mes_seguinte
                media_por_mkt = media_por_mkt.sort_values("projecao_mes_seguinte", ascending=False)
                st.dataframe(
                    capitalizar_colunas(formatar_colunas_moeda(media_por_mkt, ["media_diaria", "projecao_mes_seguinte"])),
                    width='stretch', hide_index=True,
                )

        # ---- Top produtos, separado por marketplace ----
        st.subheader("Top produtos por marketplace")
        criterio = st.radio("Ordenar por", ["Faturamento", "Unidades vendidas"], horizontal=True)
        coluna_ordenacao = "faturamento" if criterio == "Faturamento" else "unidades"

        itens_filtrados = itens[itens["pedido_id"].isin(ids_filtrados)].copy()
        itens_filtrados["valor_total_item"] = itens_filtrados["quantidade"] * itens_filtrados["valor_unitario"]
        itens_com_mkt = itens_filtrados.merge(
            pedidos_filtrados[["id", "marketplace"]], left_on="pedido_id", right_on="id", how="left"
        )

        if marketplaces_selecionados:
            abas_top = st.tabs(marketplaces_selecionados)
            for aba, mkt in zip(abas_top, marketplaces_selecionados):
                with aba:
                    itens_do_mkt = itens_com_mkt[itens_com_mkt["marketplace"] == mkt]
                    top_mkt = (
                        itens_do_mkt.groupby(["sku", "descricao"])
                        .agg(unidades=("quantidade", "sum"), faturamento=("valor_total_item", "sum"))
                        .reset_index()
                        .sort_values(coluna_ordenacao, ascending=False)
                        .head(10)
                    )
                    if top_mkt.empty:
                        st.info(f"Nenhum item vendido em {mkt} no periodo selecionado.")
                    else:
                        st.dataframe(
                            capitalizar_colunas(formatar_colunas_moeda(top_mkt, ["faturamento"])),
                            width='stretch', hide_index=True,
                        )
        else:
            st.info("Selecione ao menos um marketplace no filtro acima.")


# ------------------------- Aba de Estoque -------------------------

with aba_estoque:
    if not os.path.exists(BANCO_ESTOQUE):
        st.warning(
            f"O arquivo {BANCO_ESTOQUE} ainda nao existe nesta pasta. "
            "Rode o sincronizar_estoque_tiny.py primeiro."
        )
    else:
        estoque = carregar_estoque()

        tabela = estoque.pivot_table(
            index=["sku", "descricao"],
            columns="deposito",
            values="saldo",
            aggfunc="sum",
            fill_value=0,
        ).reset_index()
        colunas_deposito = [c for c in tabela.columns if c not in ("sku", "descricao")]
        tabela["Total Geral"] = tabela[colunas_deposito].sum(axis=1)

        em_ruptura = tabela[tabela["Total Geral"] <= 0]

        col1, col2, col3 = st.columns(3)
        col1.metric("Produtos ativos", f"{len(tabela)}")
        col2.metric("Produtos em ruptura", f"{len(em_ruptura)}")
        col3.metric("Estoque total (todas as origens)", f"{int(tabela['Total Geral'].sum())} un.")

        # ---- Giro de estoque, dias restantes e previsao de ruptura ----
        st.subheader("Giro de estoque e previsao de ruptura")

        if not os.path.exists(BANCO_VENDAS):
            st.info(
                f"Para calcular velocidade de venda e dias restantes, preciso tambem do "
                f"{BANCO_VENDAS}. Rode o sincronizar_tiny.py."
            )
        else:
            pedidos_giro, itens_giro = carregar_vendas()
            pedidos_validos_giro = pedidos_giro[~pedidos_giro["situacao"].isin(SITUACOES_EXCLUIDAS)].copy()
            pedidos_validos_giro["_data"] = pd.to_datetime(
                pedidos_validos_giro["data_pedido"], format="%d/%m/%Y", errors="coerce"
            )

            col_j1, col_j2 = st.columns(2)
            janela_dias = col_j1.slider(
                "Calcular velocidade de venda com base nos ultimos N dias", 7, 90, 30
            )
            dias_critico = col_j2.slider(
                "Considerar 'risco de ruptura' quando faltar menos de N dias", 1, 30, 7
            )
            dias_excesso = st.slider(
                "Considerar 'estoque excessivo' quando durar mais de N dias", 30, 365, 90
            )

            data_max_giro = pedidos_validos_giro["_data"].max()
            if pd.isna(data_max_giro):
                st.info("Nao ha pedidos validos suficientes para calcular a velocidade de venda.")
            else:
                data_corte = data_max_giro - pd.Timedelta(days=janela_dias - 1)
                pedidos_recentes = pedidos_validos_giro[pedidos_validos_giro["_data"] >= data_corte]
                ids_recentes = set(pedidos_recentes["id"])
                itens_recentes = itens_giro[itens_giro["pedido_id"].isin(ids_recentes)]

                velocidade = (
                    itens_recentes.groupby("sku")
                    .agg(unidades_periodo=("quantidade", "sum"))
                    .reset_index()
                )
                velocidade["velocidade_diaria"] = velocidade["unidades_periodo"] / janela_dias

                giro = tabela[["sku", "descricao", "Total Geral"]].rename(
                    columns={"Total Geral": "estoque_total"}
                ).merge(
                    velocidade[["sku", "velocidade_diaria", "unidades_periodo"]], on="sku", how="left"
                )
                giro["velocidade_diaria"] = giro["velocidade_diaria"].fillna(0)
                giro["unidades_periodo"] = giro["unidades_periodo"].fillna(0)

                giro["dias_restantes"] = giro.apply(
                    lambda r: (r["estoque_total"] / r["velocidade_diaria"])
                    if (r["velocidade_diaria"] > 0 and r["estoque_total"] > 0) else None,
                    axis=1,
                )

                def calcular_status(row):
                    if row["estoque_total"] <= 0:
                        return "Ruptura"
                    if pd.isna(row["dias_restantes"]):
                        return "Sem giro"
                    if row["dias_restantes"] <= dias_critico:
                        return "Critico"
                    if row["dias_restantes"] >= dias_excesso:
                        return "Excesso"
                    return "Saudavel"

                giro["status"] = giro.apply(calcular_status, axis=1)

                def calcular_previsao(row):
                    if row["status"] == "Ruptura":
                        return "Ja em ruptura"
                    if pd.notna(row["dias_restantes"]):
                        return (datetime.now() + pd.Timedelta(days=row["dias_restantes"])).strftime("%d/%m/%Y")
                    return "—"

                giro["previsao_ruptura"] = giro.apply(calcular_previsao, axis=1)

                contagem = giro["status"].value_counts()
                col_s1, col_s2, col_s3, col_s4, col_s5 = st.columns(5)
                col_s1.metric("Critico", int(contagem.get("Critico", 0)))
                col_s2.metric("Ruptura", int(contagem.get("Ruptura", 0)))
                col_s3.metric("Sem giro", int(contagem.get("Sem giro", 0)))
                col_s4.metric("Excesso", int(contagem.get("Excesso", 0)))
                col_s5.metric("Saudavel", int(contagem.get("Saudavel", 0)))

                def cor_status(valor):
                    cores = {
                        "Ruptura": "background-color:#7F1D1D;color:#FECACA;font-weight:600;",
                        "Critico": "background-color:#7C2D12;color:#FED7AA;font-weight:600;",
                        "Sem giro": "background-color:#374151;color:#D1D5DB;font-weight:600;",
                        "Excesso": "background-color:#78350F;color:#FDE68A;font-weight:600;",
                        "Saudavel": "background-color:#064E3B;color:#A7F3D0;font-weight:600;",
                    }
                    return cores.get(valor, "")

                aba_filtro = st.radio(
                    "Mostrar", ["Todos", "Critico + Ruptura", "Excesso", "Sem giro"], horizontal=True
                )
                if aba_filtro == "Critico + Ruptura":
                    giro_exibicao = giro[giro["status"].isin(["Critico", "Ruptura"])]
                elif aba_filtro == "Excesso":
                    giro_exibicao = giro[giro["status"] == "Excesso"]
                elif aba_filtro == "Sem giro":
                    giro_exibicao = giro[giro["status"] == "Sem giro"]
                else:
                    giro_exibicao = giro

                giro_exibicao = giro_exibicao.sort_values("dias_restantes", na_position="last")

                giro_para_exibir = capitalizar_colunas(
                    giro_exibicao[
                        ["sku", "descricao", "estoque_total", "unidades_periodo", "velocidade_diaria",
                         "dias_restantes", "status"]
                    ]
                )
                styler_giro = (
                    giro_para_exibir
                    .style.format({
                        "Velocidade Diaria": "{:.2f}",
                        "Dias Restantes": lambda v: f"{v:.0f} dias" if pd.notna(v) else "—",
                    })
                    .map(cor_status, subset=["Status"])
                )
                st.dataframe(styler_giro, width='stretch', hide_index=True)

        # ---- Faixa de estoque baixo (ajustavel) ----
        st.subheader("Produtos com estoque baixo")
        limite_baixo = st.slider(
            "Considerar 'estoque baixo' quando o total for menor ou igual a:",
            min_value=0, max_value=50, value=5,
        )
        estoque_baixo = tabela[
            (tabela["Total Geral"] > 0) & (tabela["Total Geral"] <= limite_baixo)
        ].sort_values("Total Geral")
        st.caption(f"{len(estoque_baixo)} produto(s) com estoque entre 1 e {limite_baixo} unidades.")
        st.dataframe(capitalizar_colunas(estoque_baixo), width='stretch', hide_index=True)

        # ---- Tabela completa com filtro por deposito e busca ----
        st.subheader("Estoque por produto e deposito")
        col_busca, col_dep = st.columns([2, 1])
        busca = col_busca.text_input("Filtrar por SKU ou descricao")
        deposito_foco = col_dep.selectbox("Ordenar priorizando deposito", ["Total Geral"] + colunas_deposito)

        tabela_filtrada = tabela
        if busca:
            filtro = (
                tabela["sku"].str.contains(busca, case=False, na=False)
                | tabela["descricao"].str.contains(busca, case=False, na=False)
            )
            tabela_filtrada = tabela[filtro]

        st.dataframe(
            capitalizar_colunas(tabela_filtrada.sort_values(deposito_foco, ascending=False)),
            width='stretch',
            hide_index=True,
        )

        st.subheader(f"Produtos em ruptura ({len(em_ruptura)})")
        st.dataframe(capitalizar_colunas(em_ruptura), width='stretch', hide_index=True)


# ------------------------- Aba de Devolucao -------------------------

with aba_devolucao:
    if not os.path.exists(BANCO_DEVOLUCOES):
        st.warning(
            f"O arquivo {BANCO_DEVOLUCOES} ainda nao existe nesta pasta. "
            "Rode o sincronizar_devolucoes_tiny.py primeiro."
        )
    elif not os.path.exists(BANCO_VENDAS):
        st.warning(
            f"Precisamos tambem do {BANCO_VENDAS} (vendas) para calcular o percentual "
            "de devolucao por produto. Rode o sincronizar_tiny.py primeiro."
        )
    else:
        devolucoes, itens_devolucao = carregar_devolucoes()
        pedidos_dev, itens_dev_base = carregar_vendas()
        pedidos_validos_dev = pedidos_dev[~pedidos_dev["situacao"].isin(SITUACOES_EXCLUIDAS)]
        ids_validos_dev = set(pedidos_validos_dev["id"])
        itens_vendidos = itens_dev_base[itens_dev_base["pedido_id"].isin(ids_validos_dev)].copy()
        itens_vendidos["valor_total_item"] = itens_vendidos["quantidade"] * itens_vendidos["valor_unitario"]

        faturamento_total_vendas = itens_vendidos["valor_total_item"].sum()
        unidades_totais_vendidas = itens_vendidos["quantidade"].sum()

        valor_total_devolvido = devolucoes["valor_nota"].sum()
        unidades_totais_devolvidas = itens_devolucao["quantidade"].sum()

        pct_valor = (valor_total_devolvido / faturamento_total_vendas * 100) if faturamento_total_vendas else 0
        pct_unidades = (unidades_totais_devolvidas / unidades_totais_vendidas * 100) if unidades_totais_vendidas else 0

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Devolucoes registradas", f"{len(devolucoes)}")
        col2.metric("Valor devolvido", formatar_moeda(valor_total_devolvido))

        def cor_kpi_percentual(v):
            if v >= 15:
                return "#F87171"
            elif v >= 5:
                return "#FBBF24"
            return "#34D399"

        cartao_metrica(col3, "% devolvido (valor)", formatar_percentual(pct_valor), cor_kpi_percentual(pct_valor))
        cartao_metrica(col4, "% devolvido (unidades)", formatar_percentual(pct_unidades), cor_kpi_percentual(pct_unidades))

        st.caption(
            "O percentual compara o total devolvido com o total vendido em todo o periodo "
            "sincronizado de cada banco (nao aplica os filtros da aba Vendas)."
        )

        # ---- Ranking de produtos com mais devolucao ----
        st.subheader("Ranking de produtos por devolucao")

        vendidos_por_sku = (
            itens_vendidos.groupby("sku")
            .agg(unidades_vendidas=("quantidade", "sum"))
            .reset_index()
        )
        devolvidos_por_sku = (
            itens_devolucao.groupby(["sku", "descricao"])
            .agg(unidades_devolvidas=("quantidade", "sum"), valor_devolvido=("valor_total", "sum"))
            .reset_index()
        )

        ranking = devolvidos_por_sku.merge(vendidos_por_sku, on="sku", how="left")
        ranking["unidades_vendidas"] = ranking["unidades_vendidas"].fillna(0)
        ranking["% devolucao (unidades)"] = ranking.apply(
            lambda linha: (linha["unidades_devolvidas"] / linha["unidades_vendidas"] * 100)
            if linha["unidades_vendidas"] > 0 else None,
            axis=1,
        )

        criterio_ranking = st.radio(
            "Ordenar ranking por", ["Valor devolvido", "Unidades devolvidas", "% de devolucao"], horizontal=True
        )
        mapa_coluna = {
            "Valor devolvido": "valor_devolvido",
            "Unidades devolvidas": "unidades_devolvidas",
            "% de devolucao": "% devolucao (unidades)",
        }
        ranking_ordenado = ranking.sort_values(mapa_coluna[criterio_ranking], ascending=False, na_position="last")

        ranking_para_exibir = capitalizar_colunas(
            ranking_ordenado[
                ["sku", "descricao", "unidades_devolvidas", "unidades_vendidas", "% devolucao (unidades)", "valor_devolvido"]
            ]
        )
        styler_ranking = (
            ranking_para_exibir
            .style.format({
                "Valor Devolvido": formatar_moeda,
                "% Devolucao (Unidades)": lambda v: formatar_percentual(v) if pd.notna(v) else "N/D",
            })
            .map(cor_percentual_devolucao, subset=["% Devolucao (Unidades)"])
        )
        st.dataframe(styler_ranking, width='stretch', hide_index=True)


# ------------------------- Aba de Calendario (Envios) -------------------------

with aba_calendario:
    st.caption("Agenda manual de envios para Mercado Livre Full, Amazon FBA e Shopee.")
    conexao_envios = preparar_banco_envios()

    DESTINOS = ["Mercado Livre Full", "Amazon FBA", "Shopee"]
    STATUS_OPCOES = ["Planejado", "Em separacao", "Pronto"]
    CORES_DESTINO = {
        "Mercado Livre Full": "#FBBF24",
        "Amazon FBA": "#60A5FA",
        "Shopee": "#F87171",
    }

    # ---- Formulario de novo envio ----
    with st.expander("+ Novo envio", expanded=False):
        with st.form("novo_envio_form", clear_on_submit=True):
            col_a, col_b, col_c = st.columns(3)
            data_envio = col_a.date_input("Data prevista")
            destino_envio = col_b.selectbox("Destino", DESTINOS)
            status_envio = col_c.selectbox("Status inicial", STATUS_OPCOES)
            transferencia_envio = st.radio(
                "Ja teve transferencia de estoque?", ["Nao", "Sim"], horizontal=True
            )
            observacao_envio = st.text_input("Observacao (opcional)")

            st.caption("Produtos deste envio (adicione quantas linhas precisar):")
            itens_editados = st.data_editor(
                pd.DataFrame({"sku": [""], "descricao": [""], "quantidade": [0]}),
                num_rows="dynamic",
                width='stretch',
                key="editor_novo_envio",
            )

            enviar_form = st.form_submit_button("Salvar envio")

            if enviar_form:
                cursor = conexao_envios.cursor()
                cursor.execute(
                    "INSERT INTO envios (data_prevista, destino, status, observacao, transferencia_estoque) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (data_envio.strftime("%d/%m/%Y"), destino_envio, status_envio, observacao_envio, transferencia_envio),
                )
                envio_id_novo = cursor.lastrowid
                for _, linha in itens_editados.iterrows():
                    if str(linha.get("sku", "")).strip():
                        cursor.execute(
                            "INSERT INTO envio_itens (envio_id, sku, descricao, quantidade) VALUES (?, ?, ?, ?)",
                            (envio_id_novo, linha["sku"], linha["descricao"], float(linha["quantidade"] or 0)),
                        )
                conexao_envios.commit()
                st.success("Envio salvo!")
                st.rerun()

    # ---- Lista de envios ----
    envios_df = pd.read_sql_query("SELECT * FROM envios", conexao_envios)
    itens_envio_df = pd.read_sql_query("SELECT * FROM envio_itens", conexao_envios)

    if envios_df.empty:
        st.info("Nenhum envio cadastrado ainda. Use o '+ Novo envio' acima para comecar.")
    else:
        envios_df["_data"] = pd.to_datetime(envios_df["data_prevista"], format="%d/%m/%Y", errors="coerce")
        envios_filtrados = envios_df.sort_values("_data")

        hoje_ts = pd.Timestamp(datetime.now().date())
        atrasados = envios_filtrados[
            (envios_filtrados["_data"] < hoje_ts) & (envios_filtrados["status"] != "Pronto")
        ]
        if len(atrasados) > 0:
            st.error(f"{len(atrasados)} envio(s) com data prevista no passado e ainda nao pronto(s).")

        st.subheader("Envios")
        for _, envio in envios_filtrados.iterrows():
            itens_desse_envio = itens_envio_df[itens_envio_df["envio_id"] == envio["id"]]
            cor_borda = CORES_DESTINO.get(envio["destino"], "#9FB4C7")
            esta_atrasado = envio["_data"] < hoje_ts and envio["status"] != "Pronto"
            aviso_atraso = " &nbsp;🔴 ATRASADO" if esta_atrasado else ""

            teve_transferencia = envio.get("transferencia_estoque", "Nao") == "Sim"
            bolinha = "🟢" if teve_transferencia else "🔴"
            texto_transferencia = "Transferencia de estoque: Sim" if teve_transferencia else "Transferencia de estoque: Nao"

            st.markdown(f"""
                <div style="background:{COR_FUNDO_CARD};border:1px solid rgba(255,255,255,0.08);
                            border-left:5px solid {cor_borda};border-radius:8px;
                            padding:0.9rem 1.1rem;margin-bottom:0.4rem;">
                    <b style="color:{COR_TEXTO};">{envio['data_prevista']} — {envio['destino']}</b>{aviso_atraso}<br>
                    <span style="color:{COR_TEXTO_SECUNDARIO};">Status atual: {envio['status']}</span><br>
                    <span style="color:{COR_TEXTO_SECUNDARIO};">{bolinha} {texto_transferencia}</span>
                    {f"<br><span style='color:{COR_TEXTO_SECUNDARIO};font-size:0.85rem;'>{envio['observacao']}</span>" if envio['observacao'] else ""}
                </div>
            """, unsafe_allow_html=True)

            if not itens_desse_envio.empty:
                st.dataframe(
                    capitalizar_colunas(itens_desse_envio[["sku", "descricao", "quantidade"]]),
                    hide_index=True, width='stretch',
                )

            col_upd1, col_upd2, col_upd3, col_upd4 = st.columns([2, 1, 2, 1])
            indice_status_atual = STATUS_OPCOES.index(envio["status"]) if envio["status"] in STATUS_OPCOES else 0
            novo_status = col_upd1.selectbox(
                "Atualizar status", STATUS_OPCOES, index=indice_status_atual,
                key=f"status_envio_{envio['id']}", label_visibility="collapsed",
            )
            if col_upd2.button("Salvar status", key=f"botao_status_{envio['id']}"):
                cursor = conexao_envios.cursor()
                cursor.execute("UPDATE envios SET status = ? WHERE id = ?", (novo_status, envio["id"]))
                conexao_envios.commit()
                st.rerun()

            nova_transferencia = col_upd3.radio(
                "Transferencia de estoque", ["Nao", "Sim"],
                index=1 if teve_transferencia else 0,
                key=f"transf_envio_{envio['id']}", horizontal=True, label_visibility="collapsed",
            )
            if col_upd4.button("Salvar", key=f"botao_transf_{envio['id']}"):
                cursor = conexao_envios.cursor()
                cursor.execute(
                    "UPDATE envios SET transferencia_estoque = ? WHERE id = ?", (nova_transferencia, envio["id"])
                )
                conexao_envios.commit()
                st.rerun()

            st.divider()

    conexao_envios.close()


# ------------------------- Aba de Inteligencia -------------------------

with aba_inteligencia:
    st.caption(
        "Recomendacoes calculadas a partir da velocidade de venda recente e do estoque atual. "
        "Sao sugestoes -- ajuste os parametros abaixo para o seu contexto real e use seu julgamento."
    )

    if not os.path.exists(BANCO_VENDAS) or not os.path.exists(BANCO_ESTOQUE):
        st.warning(
            f"Preciso do {BANCO_VENDAS} e do {BANCO_ESTOQUE} para calcular recomendacoes. "
            "Rode as sincronizacoes de vendas e estoque primeiro."
        )
    else:
        pedidos_int, itens_int = carregar_vendas()
        estoque_int = carregar_estoque()

        pedidos_validos_int = pedidos_int[~pedidos_int["situacao"].isin(SITUACOES_EXCLUIDAS)].copy()
        pedidos_validos_int["_data"] = pd.to_datetime(
            pedidos_validos_int["data_pedido"], format="%d/%m/%Y", errors="coerce"
        )

        tabela_estoque_int = estoque_int.pivot_table(
            index=["sku", "descricao"], columns="deposito", values="saldo", aggfunc="sum", fill_value=0
        ).reset_index()
        colunas_deposito_int = [c for c in tabela_estoque_int.columns if c not in ("sku", "descricao")]
        tabela_estoque_int["estoque_total"] = tabela_estoque_int[colunas_deposito_int].sum(axis=1)

        sub_compra, sub_envio = st.tabs(["Recomendacao de Compra", "Recomendacao de Envio (Full/FBA)"])

        # ============ Sub-aba: Recomendacao de Compra ============
        with sub_compra:
            JANELA_VELOCIDADE_FIXA = 30
            janela_compra = JANELA_VELOCIDADE_FIXA

            col_p2, col_p3, col_p4 = st.columns(3)
            lead_time_dias = col_p2.number_input(
                "Lead time do fornecedor (dias)", min_value=1, max_value=365, value=140
            )
            estoque_seguranca_dias = col_p3.number_input(
                "Estoque de seguranca (dias)", min_value=0, max_value=90, value=15
            )
            dias_cobertura_alvo = col_p4.number_input(
                "Cobertura alvo apos compra (dias)", min_value=30, max_value=365, value=180
            )
            st.caption(f"Velocidade de venda calculada com base nos ultimos {JANELA_VELOCIDADE_FIXA} dias.")

            data_max_int = pedidos_validos_int["_data"].max()
            if pd.isna(data_max_int):
                st.info("Nao ha pedidos validos suficientes para calcular a recomendacao de compra.")
            else:
                data_corte_int = data_max_int - pd.Timedelta(days=janela_compra - 1)
                recentes_int = pedidos_validos_int[pedidos_validos_int["_data"] >= data_corte_int]
                ids_recentes_int = set(recentes_int["id"])
                itens_recentes_int = itens_int[itens_int["pedido_id"].isin(ids_recentes_int)]

                velocidade_int = (
                    itens_recentes_int.groupby("sku")
                    .agg(unidades_periodo=("quantidade", "sum"))
                    .reset_index()
                )
                velocidade_int["velocidade_diaria"] = velocidade_int["unidades_periodo"] / janela_compra

                compra = tabela_estoque_int[["sku", "descricao", "estoque_total"]].merge(
                    velocidade_int[["sku", "velocidade_diaria"]], on="sku", how="left"
                )
                compra["velocidade_diaria"] = compra["velocidade_diaria"].fillna(0)

                compra["dias_restantes"] = compra.apply(
                    lambda r: (r["estoque_total"] / r["velocidade_diaria"])
                    if (r["velocidade_diaria"] > 0 and r["estoque_total"] > 0) else None,
                    axis=1,
                )

                ponto_pedido_dias = lead_time_dias + estoque_seguranca_dias

                def precisa_comprar(row):
                    if row["velocidade_diaria"] <= 0:
                        return False
                    if row["dias_restantes"] is None:
                        return True
                    return row["dias_restantes"] <= ponto_pedido_dias

                compra["precisa_comprar"] = compra.apply(precisa_comprar, axis=1)

                def quantidade_sugerida_compra(row):
                    if row["velocidade_diaria"] <= 0:
                        return 0
                    alvo = row["velocidade_diaria"] * dias_cobertura_alvo
                    sugestao = alvo - row["estoque_total"]
                    return max(0, math.ceil(sugestao))

                compra["quantidade_sugerida"] = compra.apply(quantidade_sugerida_compra, axis=1)

                def quando_comprar(row):
                    if not row["precisa_comprar"]:
                        return "—"
                    if row["dias_restantes"] is None:
                        return "COMPRAR AGORA (sem estoque)"
                    dias_para_comprar = row["dias_restantes"] - ponto_pedido_dias
                    if dias_para_comprar <= 0:
                        return "COMPRAR AGORA"
                    return (datetime.now() + pd.Timedelta(days=dias_para_comprar)).strftime("%d/%m/%Y")

                compra["quando_comprar"] = compra.apply(quando_comprar, axis=1)

                precisam = compra[compra["precisa_comprar"]].sort_values(
                    "dias_restantes", na_position="first"
                )

                col_m1, col_m2, col_m3 = st.columns(3)
                col_m1.metric("Produtos para comprar agora", int((precisam["quando_comprar"] == "COMPRAR AGORA").sum() + (precisam["quando_comprar"] == "COMPRAR AGORA (sem estoque)").sum()))
                col_m2.metric("Total de SKUs sinalizados", len(precisam))
                col_m3.metric("Unidades sugeridas (soma)", int(precisam["quantidade_sugerida"].sum()))

                st.subheader(f"Produtos que precisam de compra ({len(precisam)})")
                if precisam.empty:
                    st.success("Nenhum produto precisa de compra agora, com os parametros atuais.")
                else:
                    def cor_urgencia(valor):
                        if valor in ("COMPRAR AGORA", "COMPRAR AGORA (sem estoque)"):
                            return "background-color:#7F1D1D;color:#FECACA;font-weight:600;"
                        return "background-color:#78350F;color:#FDE68A;font-weight:600;"

                    compra_para_exibir = capitalizar_colunas(
                        precisam[["sku", "descricao", "estoque_total", "velocidade_diaria",
                                  "dias_restantes", "quantidade_sugerida", "quando_comprar"]]
                    )
                    styler_compra = (
                        compra_para_exibir
                        .style.format({
                            "Velocidade Diaria": "{:.2f}",
                            "Dias Restantes": lambda v: f"{v:.0f} dias" if pd.notna(v) else "—",
                        })
                        .map(cor_urgencia, subset=["Quando Comprar"])
                    )
                    st.dataframe(styler_compra, width='stretch', hide_index=True)

        # ============ Sub-aba: Recomendacao de Envio (Full/FBA) ============
        with sub_envio:
            def classificar_grupo(nome_mkt):
                nome = (nome_mkt or "").lower()
                if "mercado livre" in nome or nome.startswith("ml"):
                    return "Mercado Livre"
                if "amazon" in nome:
                    return "Amazon"
                if "shopee" in nome:
                    return "Shopee"
                if "tiktok" in nome:
                    return "TikTok"
                return "Outro"

            janela_envio = 30
            col_e2, col_e3 = st.columns(2)
            cobertura_full_dias = col_e2.number_input(
                "Cobertura alvo no Mercado Livre Full (dias)", min_value=1, max_value=180, value=20
            )
            cobertura_fba_dias = col_e3.number_input(
                "Cobertura alvo na Amazon FBA (dias)", min_value=1, max_value=180, value=20
            )

            if "Mercado Livre Full" not in colunas_deposito_int and "Amazon FBA" not in colunas_deposito_int:
                st.info(
                    "Nao encontrei depositos chamados 'Mercado Livre Full' ou 'Amazon FBA' no seu estoque "
                    "sincronizado. Confira os nomes exatos dos depositos na aba Estoque."
                )
            else:
                pedidos_validos_int["grupo"] = pedidos_validos_int["marketplace"].apply(classificar_grupo)
                data_max_env = pedidos_validos_int["_data"].max()
                data_corte_env = data_max_env - pd.Timedelta(days=janela_envio - 1)
                recentes_env = pedidos_validos_int[pedidos_validos_int["_data"] >= data_corte_env]

                itens_com_grupo = itens_int.merge(
                    recentes_env[["id", "grupo"]], left_on="pedido_id", right_on="id", how="inner"
                )
                velocidade_grupo = (
                    itens_com_grupo.groupby(["sku", "grupo"]).agg(unidades=("quantidade", "sum")).reset_index()
                )
                velocidade_grupo["velocidade_diaria"] = velocidade_grupo["unidades"] / janela_envio
                pivot_vel = velocidade_grupo.pivot_table(
                    index="sku", columns="grupo", values="velocidade_diaria", fill_value=0
                ).reset_index()

                envio = tabela_estoque_int.merge(pivot_vel, on="sku", how="left")
                for col_necessaria in ["Mercado Livre", "Amazon", "Mercado Livre Full", "Amazon FBA", "Geral"]:
                    if col_necessaria not in envio.columns:
                        envio[col_necessaria] = 0
                envio = envio.fillna(0)

                envio["envio_sugerido_full"] = (
                    envio["Mercado Livre"] * cobertura_full_dias - envio["Mercado Livre Full"]
                ).apply(lambda v: max(0, math.ceil(v)))
                envio["envio_sugerido_fba"] = (
                    envio["Amazon"] * cobertura_fba_dias - envio["Amazon FBA"]
                ).apply(lambda v: max(0, math.ceil(v)))

                envio["envio_sugerido_full"] = envio[["envio_sugerido_full", "Geral"]].min(axis=1).clip(lower=0)
                envio["envio_sugerido_fba"] = envio[["envio_sugerido_fba", "Geral"]].min(axis=1).clip(lower=0)

                st.caption(
                    "O envio sugerido nunca ultrapassa o que voce tem disponivel no deposito 'Geral' (estoque proprio). "
                    "Se sugerir envio para os dois destinos ao mesmo tempo, confira se o total nao excede seu estoque real "
                    "antes de separar -- o calculo aqui nao divide automaticamente entre os dois."
                )

                col_r1, col_r2 = st.columns(2)

                with col_r1:
                    st.subheader("Enviar para Mercado Livre Full")
                    sugestao_full = envio[envio["envio_sugerido_full"] > 0][
                        ["sku", "descricao", "Geral", "Mercado Livre Full", "Mercado Livre", "envio_sugerido_full"]
                    ].sort_values("envio_sugerido_full", ascending=False)
                    sugestao_full = sugestao_full.rename(columns={
                        "Mercado Livre": "velocidade_ml_dia", "Mercado Livre Full": "estoque_full_atual",
                    })
                    if sugestao_full.empty:
                        st.success("Nenhum envio sugerido para o Full agora.")
                    else:
                        sugestao_full_exibir = capitalizar_colunas(sugestao_full)
                        st.dataframe(
                            sugestao_full_exibir.style.format({"Velocidade Ml Dia": "{:.2f}"}),
                            width='stretch', hide_index=True,
                        )
                        st.metric("Total sugerido para o Full", f"{int(sugestao_full['envio_sugerido_full'].sum())} un.")

                with col_r2:
                    st.subheader("Enviar para Amazon FBA")
                    sugestao_fba = envio[envio["envio_sugerido_fba"] > 0][
                        ["sku", "descricao", "Geral", "Amazon FBA", "Amazon", "envio_sugerido_fba"]
                    ].sort_values("envio_sugerido_fba", ascending=False)
                    sugestao_fba = sugestao_fba.rename(columns={
                        "Amazon": "velocidade_amazon_dia", "Amazon FBA": "estoque_fba_atual",
                    })
                    if sugestao_fba.empty:
                        st.success("Nenhum envio sugerido para a FBA agora.")
                    else:
                        sugestao_fba_exibir = capitalizar_colunas(sugestao_fba)
                        st.dataframe(
                            sugestao_fba_exibir.style.format({"Velocidade Amazon Dia": "{:.2f}"}),
                            width='stretch', hide_index=True,
                        )
                        st.metric("Total sugerido para a FBA", f"{int(sugestao_fba['envio_sugerido_fba'].sum())} un.")
