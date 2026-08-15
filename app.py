"""
Servidor do Curso de Programação — versão nuvem (Render + Google Drive)
=======================================================================

Adaptação do servidor_curso.py (que rodava em LAN) para hospedagem pública.

O que mudou em relação à versão LAN:
  - Sem mDNS/zeroconf (aula.local) — agora o acesso é por URL pública HTTPS.
  - Uploads dos alunos vão para uma pasta no Google Drive (disco do Render é
    efêmero e apagaria os projetos a cada reinício/deploy).
  - Tela de senha única para a turma antes de acessar qualquer coisa.
  - Arquivos base para download também ficam no Google Drive (pasta separada).

Variáveis de ambiente necessárias (configuradas no painel do Render):
  SENHA_TURMA          -> senha única compartilhada com os alunos
  FLASK_SECRET         -> string aleatória p/ assinar a sessão (qualquer texto longo)
  GOOGLE_CREDENTIALS   -> conteúdo JSON da conta de serviço do Google (colar inteiro)
  DRIVE_PASTA_UPLOADS  -> ID da pasta do Drive onde os projetos enviados são salvos
  DRIVE_PASTA_BASE     -> ID da pasta do Drive com os arquivos base p/ download
"""

import os
import io
import json
import re
import datetime
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template_string, send_file, flash, abort
)

# --- Google Drive ---
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "troque-isto-em-producao")

SENHA_TURMA = os.environ.get("SENHA_TURMA", "cookies2026")
DRIVE_PASTA_UPLOADS = os.environ.get("DRIVE_PASTA_UPLOADS", "")
DRIVE_PASTA_BASE = os.environ.get("DRIVE_PASTA_BASE", "")

# Extensões permitidas no upload (projetos Scratch e afins)
EXTENSOES_OK = {".sb3", ".sb2", ".png", ".jpg", ".jpeg", ".zip"}
TAMANHO_MAX_MB = 25
app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAX_MB * 1024 * 1024

# Paleta do curso (roxo / laranja / verde-limão) — mesma identidade das aulas
COR_ROXO = "#7c3aed"
COR_LARANJA = "#f97316"
COR_LIMAO = "#84cc16"


# --------------------------------------------------------------------------
# Google Drive — serviço
# --------------------------------------------------------------------------

def get_drive():
    """Cria o cliente do Google Drive a partir da conta de serviço."""
    cred_json = os.environ.get("GOOGLE_CREDENTIALS", "")
    if not cred_json:
        raise RuntimeError("GOOGLE_CREDENTIALS não configurada.")
    info = json.loads(cred_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def drive_listar(pasta_id):
    """Lista arquivos (nome + id) de uma pasta do Drive."""
    if not pasta_id:
        return []
    servico = get_drive()
    resultado = servico.files().list(
        q=f"'{pasta_id}' in parents and trashed = false",
        fields="files(id, name, size, modifiedTime)",
        orderBy="name",
        pageSize=200,
    ).execute()
    return resultado.get("files", [])


def drive_enviar(pasta_id, nome_arquivo, dados_bytes, mimetype):
    """Envia bytes para uma pasta do Drive."""
    servico = get_drive()
    meta = {"name": nome_arquivo, "parents": [pasta_id]}
    media = MediaIoBaseUpload(io.BytesIO(dados_bytes), mimetype=mimetype, resumable=False)
    servico.files().create(body=meta, media_body=media, fields="id").execute()


def drive_baixar(arquivo_id):
    """Baixa um arquivo do Drive e devolve (bytes, nome)."""
    servico = get_drive()
    meta = servico.files().get(fileId=arquivo_id, fields="name").execute()
    buffer = io.BytesIO()
    req = servico.files().get_media(fileId=arquivo_id)
    downloader = MediaIoBaseDownload(buffer, req)
    concluido = False
    while not concluido:
        _, concluido = downloader.next_chunk()
    buffer.seek(0)
    return buffer, meta["name"]


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------

def limpar_nome(texto):
    """Deixa o nome do aluno seguro para virar nome de arquivo."""
    texto = texto.strip()
    texto = re.sub(r"[^\w\s-]", "", texto, flags=re.UNICODE)
    texto = re.sub(r"[\s]+", "_", texto)
    return texto[:40] or "aluno"


def login_obrigatorio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------
# Templates (HTML embutido para manter tudo em 1 arquivo)
# --------------------------------------------------------------------------

BASE_CSS = f"""
  :root {{
    --roxo: {COR_ROXO};
    --laranja: {COR_LARANJA};
    --limao: {COR_LIMAO};
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Nunito', 'Segoe UI', system-ui, sans-serif;
    background: linear-gradient(135deg, #faf5ff 0%, #fff7ed 100%);
    color: #1f2937;
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px 16px;
  }}
  .cartao {{
    background: #fff;
    border-radius: 24px;
    box-shadow: 0 10px 40px rgba(124,58,237,0.15);
    padding: 32px;
    width: 100%;
    max-width: 640px;
    margin-bottom: 24px;
  }}
  h1 {{ color: var(--roxo); font-size: 1.9rem; margin-bottom: 4px; }}
  h2 {{ color: var(--laranja); font-size: 1.2rem; margin: 20px 0 12px; }}
  .subtitulo {{ color: #6b7280; margin-bottom: 20px; }}
  label {{ display: block; font-weight: 700; margin: 14px 0 6px; color: #374151; }}
  input[type=text], input[type=password], input[type=file] {{
    width: 100%;
    padding: 14px;
    border: 2px solid #e5e7eb;
    border-radius: 14px;
    font-size: 1rem;
  }}
  input:focus {{ outline: none; border-color: var(--roxo); }}
  button {{
    background: var(--roxo);
    color: #fff;
    border: none;
    border-radius: 14px;
    padding: 14px 24px;
    font-size: 1.05rem;
    font-weight: 800;
    cursor: pointer;
    width: 100%;
    margin-top: 20px;
    transition: transform .1s, background .2s;
  }}
  button:hover {{ background: #6d28d9; transform: translateY(-2px); }}
  .botao-limao {{ background: var(--limao); }}
  .botao-limao:hover {{ background: #65a30d; }}
  .lista-arquivos {{ list-style: none; }}
  .lista-arquivos li {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 16px;
    border: 2px solid #f3f4f6;
    border-radius: 12px;
    margin-bottom: 8px;
  }}
  .lista-arquivos a {{
    background: var(--laranja);
    color: #fff;
    text-decoration: none;
    padding: 8px 16px;
    border-radius: 10px;
    font-weight: 700;
    font-size: .9rem;
  }}
  .flash {{
    background: #ecfccb;
    border-left: 5px solid var(--limao);
    padding: 14px;
    border-radius: 10px;
    margin-bottom: 16px;
    font-weight: 600;
  }}
  .flash.erro {{ background: #fee2e2; border-color: #ef4444; }}
  .rodape {{ color: #9ca3af; font-size: .85rem; text-align: center; }}
  .emoji {{ font-size: 2.4rem; }}
"""

PAGINA_LOGIN = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Curso de Programação — Entrar</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="cartao">
    <div class="emoji">🍪🔒</div>
    <h1>Curso de Programação</h1>
    <p class="subtitulo">Digite a senha da turma para entrar.</p>
    {% with mensagens = get_flashed_messages(with_categories=true) %}
      {% for categoria, msg in mensagens %}
        <div class="flash {{ categoria }}">{{ msg }}</div>
      {% endfor %}
    {% endwith %}
    <form method="post">
      <label for="senha">Senha da turma</label>
      <input type="password" id="senha" name="senha" autofocus required>
      <button type="submit">Entrar 🚀</button>
    </form>
  </div>
  <p class="rodape">Feito para a nossa aula de terça 💜</p>
</body>
</html>
"""

PAGINA_INICIO = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Curso de Programação</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="cartao">
    <div class="emoji">🎮</div>
    <h1>Bem-vindo à aula!</h1>
    <p class="subtitulo">Baixe os arquivos base e envie o seu projeto aqui.</p>
    {% with mensagens = get_flashed_messages(with_categories=true) %}
      {% for categoria, msg in mensagens %}
        <div class="flash {{ categoria }}">{{ msg }}</div>
      {% endfor %}
    {% endwith %}

    <h2>⬇️ Arquivos base para baixar</h2>
    {% if arquivos_base %}
      <ul class="lista-arquivos">
        {% for arq in arquivos_base %}
          <li>
            <span>📄 {{ arq.name }}</span>
            <a href="{{ url_for('baixar', arquivo_id=arq.id) }}">Baixar</a>
          </li>
        {% endfor %}
      </ul>
    {% else %}
      <p class="subtitulo">Nenhum arquivo base disponível ainda.</p>
    {% endif %}
  </div>

  <div class="cartao">
    <h2>⬆️ Enviar o meu projeto</h2>
    <form method="post" action="{{ url_for('enviar') }}" enctype="multipart/form-data">
      <label for="nome">Seu nome</label>
      <input type="text" id="nome" name="nome" placeholder="Ex: Ana Clara" required>

      <label for="arquivo">Arquivo do projeto (.sb3)</label>
      <input type="file" id="arquivo" name="arquivo" accept=".sb3,.sb2,.png,.jpg,.jpeg,.zip" required>

      <button type="submit" class="botao-limao">Enviar projeto ✨</button>
    </form>
  </div>

  <p class="rodape">
    <a href="{{ url_for('sair') }}" style="color:#9ca3af;">Sair</a>
  </p>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Rotas
# --------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("senha") == SENHA_TURMA:
            session["autenticado"] = True
            return redirect(url_for("inicio"))
        flash("Senha incorreta. Tente de novo!", "erro")
    return render_template_string(PAGINA_LOGIN, css=BASE_CSS)


@app.route("/sair")
def sair():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_obrigatorio
def inicio():
    try:
        arquivos_base = drive_listar(DRIVE_PASTA_BASE)
    except Exception as e:
        arquivos_base = []
        flash(f"Não consegui listar os arquivos base: {e}", "erro")
    return render_template_string(PAGINA_INICIO, css=BASE_CSS, arquivos_base=arquivos_base)


@app.route("/enviar", methods=["POST"])
@login_obrigatorio
def enviar():
    nome_aluno = limpar_nome(request.form.get("nome", ""))
    arquivo = request.files.get("arquivo")

    if not arquivo or arquivo.filename == "":
        flash("Você precisa escolher um arquivo.", "erro")
        return redirect(url_for("inicio"))

    ext = os.path.splitext(arquivo.filename)[1].lower()
    if ext not in EXTENSOES_OK:
        flash(f"Tipo de arquivo não permitido ({ext}).", "erro")
        return redirect(url_for("inicio"))

    # Nome final: NomeDoAluno_projeto_AAAAMMDD_HHMM.sb3
    carimbo = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    nome_final = f"{nome_aluno}_projeto_{carimbo}{ext}"

    try:
        dados = arquivo.read()
        drive_enviar(DRIVE_PASTA_UPLOADS, nome_final, dados, arquivo.mimetype or "application/octet-stream")
        flash(f"Projeto enviado com sucesso! 🎉 ({nome_final})", "sucesso")
    except Exception as e:
        flash(f"Erro ao enviar: {e}", "erro")

    return redirect(url_for("inicio"))


@app.route("/baixar/<arquivo_id>")
@login_obrigatorio
def baixar(arquivo_id):
    try:
        buffer, nome = drive_baixar(arquivo_id)
        return send_file(buffer, as_attachment=True, download_name=nome)
    except Exception as e:
        flash(f"Erro ao baixar: {e}", "erro")
        return redirect(url_for("inicio"))


@app.route("/saude")
def saude():
    """Rota simples para manter o serviço acordado (ping externo)."""
    return "ok", 200


if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=porta)
