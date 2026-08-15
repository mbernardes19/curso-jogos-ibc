"""
gerar_token.py — RODE UMA VEZ SÓ, NA SUA MÁQUINA (não no Render).
==================================================================

Este script abre o navegador, você faz login na SUA conta do Gmail e autoriza
o acesso ao Drive. No final, ele imprime um REFRESH TOKEN.

Você vai copiar esse refresh token para a variável de ambiente
GOOGLE_REFRESH_TOKEN no Render (junto com o CLIENT_ID e CLIENT_SECRET).

Pré-requisitos:
  1. Ter o arquivo 'client_secret.json' nesta mesma pasta
     (credencial OAuth do tipo "App para computador" — veja o guia).
  2. Instalar as libs:
     pip install google-auth-oauthlib google-api-python-client

Como rodar:
     python gerar_token.py
"""

from google_auth_oauthlib.flow import InstalledAppFlow

# Escopo completo do Drive (ler, criar, baixar).
SCOPES = ["https://www.googleapis.com/auth/drive"]


def main():
    flow = InstalledAppFlow.from_client_secrets_file("client_secret.json", SCOPES)
    # Abre o navegador para você logar e autorizar.
    # access_type=offline + prompt=consent garante que venha um refresh_token.
    creds = flow.run_local_server(
        port=0,
        access_type="offline",
        prompt="consent",
    )

    print("\n" + "=" * 60)
    print("SUCESSO! Copie os valores abaixo para o Render:")
    print("=" * 60)
    print(f"\nGOOGLE_CLIENT_ID:\n{creds.client_id}\n")
    print(f"GOOGLE_CLIENT_SECRET:\n{creds.client_secret}\n")
    print(f"GOOGLE_REFRESH_TOKEN:\n{creds.refresh_token}\n")
    print("=" * 60)
    print("Guarde o refresh token com cuidado — ele dá acesso ao seu Drive.")
    print("=" * 60)


if __name__ == "__main__":
    main()
