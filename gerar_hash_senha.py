"""
Namilay OS - Criar login (e-mail + senha) para o site
------------------------------------------------------------
A senha e digitada AQUI, no seu computador (nao aparece na tela) e vira um
codigo irreversivel. So esse codigo vai para os Secrets do Streamlit.

COMO USAR:  python gerar_hash_senha.py
"""

import getpass

from autenticacao import gerar_hash


def main():
    print("=== Namilay OS - criar login ===\n")
    email = input("E-mail de quem vai entrar: ").strip().lower()
    if "@" not in email:
        print("Esse e-mail parece estranho. Tente de novo.")
        return
    senha = getpass.getpass("Senha (nao aparece enquanto voce digita): ")
    if len(senha) < 8:
        print("Use pelo menos 8 caracteres.")
        return
    if senha != getpass.getpass("Repita a senha: "):
        print("As senhas nao sao iguais. Tente de novo.")
        return

    print("\nPronto! Cole isto NO FINAL dos Secrets do app no Streamlit.")
    print("(Se for o primeiro login, cole as DUAS linhas. Para outro login depois,")
    print(" cole so a linha do e-mail, logo abaixo de [usuarios].)\n")
    print("[usuarios]")
    print('"{}" = "{}"'.format(email, gerar_hash(senha)))


if __name__ == "__main__":
    main()
