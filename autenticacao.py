"""
Namilay OS - Login simples (e-mail + senha)
------------------------------------------------------------
- A senha NUNCA fica guardada em lugar nenhum: so um "hash" (uma impressao
  digital que nao da para desfazer).
- Os hashes ficam nos Secrets do Streamlit (secao [usuarios]), nunca no GitHub.
- Para criar ou trocar uma senha, rode no seu PC: python gerar_hash_senha.py
"""

import base64
import hashlib
import hmac
import os
import time

ITERACOES = 600_000
MAX_TENTATIVAS = 5
BLOQUEIO_SEGUNDOS = 60
_HASH_FALSO = None


def gerar_hash(senha, iteracoes=ITERACOES):
    sal = os.urandom(16)
    derivado = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, iteracoes)
    return "pbkdf2_sha256${}${}${}".format(
        iteracoes,
        base64.b64encode(sal).decode("ascii"),
        base64.b64encode(derivado).decode("ascii"),
    )


def verificar_senha(senha, hash_guardado):
    try:
        algoritmo, iteracoes, sal_b64, hash_b64 = str(hash_guardado).split("$")
        if algoritmo != "pbkdf2_sha256":
            return False
        iteracoes = int(iteracoes)
        sal = base64.b64decode(sal_b64)
        esperado = base64.b64decode(hash_b64)
    except Exception:
        return False
    if iteracoes < 1:
        return False
    calculado = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, iteracoes)
    return hmac.compare_digest(calculado, esperado)


def _hash_falso():
    """Usado quando o e-mail nao existe, para gastar o mesmo tempo de uma senha errada
    (assim ninguem descobre, pelo tempo de resposta, quais e-mails existem)."""
    global _HASH_FALSO
    if _HASH_FALSO is None:
        _HASH_FALSO = gerar_hash(base64.b64encode(os.urandom(12)).decode("ascii"))
    return _HASH_FALSO


def _carregar_usuarios(st):
    """Devolve (modo, usuarios). modo: 'ok' | 'local' | 'sem_config'."""
    try:
        segredos = st.secrets
        if "usuarios" not in segredos:
            return "sem_config", {}
        bruto = dict(segredos["usuarios"])
    except Exception as erro:
        if isinstance(erro, FileNotFoundError) or "NotFound" in type(erro).__name__:
            return "local", {}     # rodando no seu PC, sem arquivo de secrets
        return "sem_config", {}
    usuarios = {str(k).strip().lower(): str(v) for k, v in bruto.items()}
    if not usuarios:
        return "sem_config", {}
    return "ok", usuarios


def exigir_login(st):
    """Mostra a tela de login e PARA o app ate a pessoa entrar.
    Devolve o e-mail de quem entrou (ou None quando roda localmente, sem secrets)."""
    modo, usuarios = _carregar_usuarios(st)

    if modo == "local":
        return None
    if modo == "sem_config":
        st.error("O login ainda não foi configurado: adicione a seção [usuarios] nos Secrets do app.")
        st.stop()

    email_sessao = st.session_state.get("auth_email")
    if email_sessao and email_sessao in usuarios:
        return email_sessao

    _, centro, _ = st.columns([1, 1.2, 1])

    agora = time.time()
    bloqueado_ate = st.session_state.get("login_bloqueado_ate", 0)
    if agora < bloqueado_ate:
        with centro:
            st.error("Muitas tentativas. Tente de novo em {} segundos.".format(int(bloqueado_ate - agora) + 1))
        st.stop()

    with centro:
        st.subheader("Entrar")
        with st.form("login_namilay"):
            email = st.text_input("E-mail")
            senha = st.text_input("Senha", type="password")
            entrou = st.form_submit_button("Entrar")

        if entrou:
            email_norm = (email or "").strip().lower()
            hash_usuario = usuarios.get(email_norm)
            confere = verificar_senha(senha or "", hash_usuario or _hash_falso())
            if confere and hash_usuario:
                st.session_state["auth_email"] = email_norm
                st.session_state["tentativas_login"] = 0
                st.rerun()
            time.sleep(1.5)
            tentativas = st.session_state.get("tentativas_login", 0) + 1
            if tentativas >= MAX_TENTATIVAS:
                st.session_state["login_bloqueado_ate"] = time.time() + BLOQUEIO_SEGUNDOS
                tentativas = 0
            st.session_state["tentativas_login"] = tentativas
            st.error("E-mail ou senha incorretos.")

    st.stop()


def mostrar_sessao(st, email):
    col_info, col_sair = st.columns([6, 1])
    col_info.caption("Conectado como {}".format(email))
    if col_sair.button("Sair", key="botao_sair_namilay"):
        for chave in ("auth_email", "tentativas_login", "login_bloqueado_ate"):
            st.session_state.pop(chave, None)
        st.rerun()
