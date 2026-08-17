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
  SENHA_TURMA            -> senha única compartilhada com os alunos
  FLASK_SECRET           -> string aleatória p/ assinar a sessão (qualquer texto longo)
  GOOGLE_CLIENT_ID       -> client_id do OAuth (gerado por gerar_token.py)
  GOOGLE_CLIENT_SECRET   -> client_secret do OAuth
  GOOGLE_REFRESH_TOKEN   -> refresh token da SUA conta (gerado por gerar_token.py)
  DRIVE_PASTA_UPLOADS    -> ID da pasta do Drive onde os projetos enviados são salvos
  DRIVE_PASTA_BASE       -> ID da pasta do Drive com os arquivos base p/ download
  ADMIN_SENHA            -> senha do painel /admin (definir o número da aula do dia)

Número da aula atual: fica salvo num arquivinho de texto dentro da pasta do
Drive de DRIVE_PASTA_UPLOADS (sobrevive a reinícios do Render, que apaga o
disco local). Você define pelo painel /admin, sem precisar fazer deploy.

Autenticação: usa OAuth com refresh token (delegação), então os arquivos
enviados vão para a SUA cota pessoal do Drive (15 GB+). Isso contorna o erro
"Service Accounts do not have storage quota" que ocorre com conta de serviço
em Gmail comum.
"""

import os
import io
import json
import re
import time
import datetime
from functools import wraps

from flask import (
    Flask, request, session, redirect, url_for,
    render_template_string, send_file, flash, abort
)

# --- Google Drive ---
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload

# --------------------------------------------------------------------------
# Configuração
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "troque-isto-em-producao")

SENHA_TURMA = os.environ.get("SENHA_TURMA", "cookies2026")
ADMIN_SENHA = os.environ.get("ADMIN_SENHA", "troque-a-senha-admin")
DRIVE_PASTA_UPLOADS = os.environ.get("DRIVE_PASTA_UPLOADS", "")
DRIVE_PASTA_BASE = os.environ.get("DRIVE_PASTA_BASE", "")

# Nome do arquivinho de texto (dentro de DRIVE_PASTA_UPLOADS) que guarda o
# número da aula atual. Fica no Drive (e não no disco do Render) porque o
# disco do Render é efêmero e o valor se perderia a cada reinício/deploy.
ARQUIVO_CONFIG_AULA = "_config_aula_atual.txt"
CACHE_AULA_TTL_SEGUNDOS = 30

# Extensões permitidas no upload (projetos Scratch e afins)
EXTENSOES_OK = {".sb3", ".sb2", ".png", ".jpg", ".jpeg", ".zip"}
TAMANHO_MAX_MB = 25
app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAX_MB * 1024 * 1024

# Tipos de projeto disponíveis no formulário de envio e a subpasta (dentro da
# pasta do aluno) correspondente a cada um.
TIPOS_PROJETO = {
    "Projeto em aula": "Aula",
    "Tarefa de casa": "Casa",
    "Projeto final": "Final",
}
TIPO_PROJETO_PADRAO = "Projeto em aula"

# Paleta do curso (roxo / laranja / verde-limão) — mesma identidade das aulas
COR_ROXO = "#7c3aed"
COR_LARANJA = "#f97316"
COR_LIMAO = "#84cc16"

# Fuso horário de Brasília (GMT-3, sem horário de verão atualmente)
TZ_BRASIL = datetime.timezone(datetime.timedelta(hours=-3), name="GMT-3")


# --------------------------------------------------------------------------
# Google Drive — serviço
# --------------------------------------------------------------------------

def get_drive():
    """Cria o cliente do Google Drive usando OAuth (refresh token).

    Os arquivos criados pertencem à SUA conta pessoal, então usam a sua cota
    de armazenamento (evita o erro 'storageQuotaExceeded' da conta de serviço).
    O access token de 1h é renovado automaticamente a partir do refresh token.
    """
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(
            "Faltam GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET ou GOOGLE_REFRESH_TOKEN."
        )
    creds = Credentials(
        token=None,  # sem access token inicial; será obtido pelo refresh
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id,
        client_secret=client_secret,
        scopes=["https://www.googleapis.com/auth/drive"],
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


def drive_obter_ou_criar_pasta(pasta_pai_id, nome_pasta):
    """Procura uma subpasta com esse nome dentro de `pasta_pai_id`.

    Se já existir (mesmo nome, mesma pasta-pai), reutiliza e devolve o id.
    Caso contrário, cria a pasta e devolve o id da pasta nova.
    """
    servico = get_drive()
    nome_escapado = nome_pasta.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"'{pasta_pai_id}' in parents and name = '{nome_escapado}' "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    resultado = servico.files().list(q=query, fields="files(id, name)", pageSize=1).execute()
    encontradas = resultado.get("files", [])
    if encontradas:
        return encontradas[0]["id"]

    meta = {
        "name": nome_pasta,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [pasta_pai_id],
    }
    pasta_nova = servico.files().create(body=meta, fields="id").execute()
    return pasta_nova["id"]


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


def _drive_buscar_arquivo(pasta_id, nome_arquivo):
    """Procura um arquivo pelo nome dentro de uma pasta. Devolve o id ou None."""
    servico = get_drive()
    nome_escapado = nome_arquivo.replace("\\", "\\\\").replace("'", "\\'")
    query = f"'{pasta_id}' in parents and name = '{nome_escapado}' and trashed = false"
    resultado = servico.files().list(q=query, fields="files(id)", pageSize=1).execute()
    encontrados = resultado.get("files", [])
    return encontrados[0]["id"] if encontrados else None


def drive_ler_texto(pasta_id, nome_arquivo):
    """Lê o conteúdo (texto) de um arquivo pequeno numa pasta do Drive.

    Devolve None se a pasta não estiver configurada ou o arquivo não existir.
    """
    if not pasta_id:
        return None
    arquivo_id = _drive_buscar_arquivo(pasta_id, nome_arquivo)
    if not arquivo_id:
        return None
    buffer, _ = drive_baixar(arquivo_id)
    return buffer.read().decode("utf-8").strip()


def drive_escrever_texto(pasta_id, nome_arquivo, conteudo):
    """Cria ou substitui o conteúdo de um arquivo de texto pequeno no Drive."""
    if not pasta_id:
        raise RuntimeError("DRIVE_PASTA_UPLOADS não está configurada.")
    servico = get_drive()
    media = MediaIoBaseUpload(
        io.BytesIO(conteudo.encode("utf-8")), mimetype="text/plain", resumable=False
    )
    arquivo_id = _drive_buscar_arquivo(pasta_id, nome_arquivo)
    if arquivo_id:
        servico.files().update(fileId=arquivo_id, media_body=media).execute()
    else:
        meta = {"name": nome_arquivo, "parents": [pasta_id]}
        servico.files().create(body=meta, media_body=media, fields="id").execute()


# --------------------------------------------------------------------------
# Aula atual (cache em memória por cima do arquivo no Drive)
# --------------------------------------------------------------------------

_cache_aula = {"valor": None, "expira": 0}


def obter_aula_atual():
    """Devolve o número da aula atual (string) ou None se não estiver definido.

    Usa um cache em memória de alguns segundos para não bater no Drive a cada
    requisição; se a leitura do Drive falhar, mantém o último valor conhecido.
    """
    agora = time.time()
    if _cache_aula["expira"] > agora:
        return _cache_aula["valor"]
    try:
        valor = drive_ler_texto(DRIVE_PASTA_UPLOADS, ARQUIVO_CONFIG_AULA) or None
        _cache_aula["valor"] = valor
    except Exception:
        pass  # mantém o último valor conhecido em caso de erro passageiro
    _cache_aula["expira"] = agora + CACHE_AULA_TTL_SEGUNDOS
    return _cache_aula["valor"]


def definir_aula_atual(valor):
    """Salva o número da aula atual no Drive e já atualiza o cache local."""
    drive_escrever_texto(DRIVE_PASTA_UPLOADS, ARQUIVO_CONFIG_AULA, valor or "")
    _cache_aula["valor"] = valor or None
    _cache_aula["expira"] = time.time() + CACHE_AULA_TTL_SEGUNDOS


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


def admin_obrigatorio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("admin_autenticado"):
            return redirect(url_for("admin_login"))
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
  input[type=text], input[type=password], input[type=file], select {{
    width: 100%;
    padding: 14px;
    border: 2px solid #e5e7eb;
    border-radius: 14px;
    font-size: 1rem;
    font-family: inherit;
    background: #fff;
  }}
  input:focus, select:focus {{ outline: none; border-color: var(--roxo); }}
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

      <label for="tipo_projeto">Tipo de projeto</label>
      <select id="tipo_projeto" name="tipo_projeto">
        {% for tipo in tipos_projeto %}
          <option value="{{ tipo }}" {% if tipo == tipo_projeto_padrao %}selected{% endif %}>{{ tipo }}</option>
        {% endfor %}
      </select>

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

PAGINA_ADMIN_LOGIN = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Painel do professor — Entrar</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="cartao">
    <div class="emoji">🧑‍🏫</div>
    <h1>Painel do professor</h1>
    <p class="subtitulo">Digite a senha do painel para definir a aula do dia.</p>
    {% with mensagens = get_flashed_messages(with_categories=true) %}
      {% for categoria, msg in mensagens %}
        <div class="flash {{ categoria }}">{{ msg }}</div>
      {% endfor %}
    {% endwith %}
    <form method="post">
      <label for="senha">Senha do painel</label>
      <input type="password" id="senha" name="senha" autofocus required>
      <button type="submit">Entrar 🚀</button>
    </form>
  </div>
</body>
</html>
"""

PAGINA_ADMIN = """
<!DOCTYPE html>
<html lang="pt-br">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Painel do professor</title>
  <style>{{ css }}</style>
</head>
<body>
  <div class="cartao">
    <div class="emoji">🧑‍🏫</div>
    <h1>Aula atual</h1>
    <p class="subtitulo">
      {% if aula_atual %}
        Aula marcada agora: <strong>{{ aula_atual }}</strong>
        (os arquivos enviados ficam com "_aula{{ aula_atual }}_" no nome).
      {% else %}
        Nenhuma aula definida no momento — os arquivos enviados não recebem marcação de aula.
      {% endif %}
    </p>
    {% with mensagens = get_flashed_messages(with_categories=true) %}
      {% for categoria, msg in mensagens %}
        <div class="flash {{ categoria }}">{{ msg }}</div>
      {% endfor %}
    {% endwith %}
    <form method="post">
      <label for="aula">Número da aula de hoje</label>
      <input type="text" id="aula" name="aula" placeholder="Ex: 3" value="{{ aula_atual or '' }}" inputmode="numeric">
      <button type="submit">Salvar aula ✅</button>
    </form>
  </div>
  <p class="rodape">
    <a href="{{ url_for('admin_sair') }}" style="color:#9ca3af;">Sair do painel</a>
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


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("senha") == ADMIN_SENHA:
            session["admin_autenticado"] = True
            return redirect(url_for("admin"))
        flash("Senha incorreta. Tente de novo!", "erro")
    return render_template_string(PAGINA_ADMIN_LOGIN, css=BASE_CSS)


@app.route("/admin/sair")
def admin_sair():
    session.pop("admin_autenticado", None)
    return redirect(url_for("admin_login"))


@app.route("/admin", methods=["GET", "POST"])
@admin_obrigatorio
def admin():
    if request.method == "POST":
        valor = request.form.get("aula", "").strip()
        if valor and not valor.isdigit():
            flash("O número da aula deve conter só dígitos (ou deixe vazio para remover).", "erro")
        else:
            try:
                definir_aula_atual(valor)
                flash("Aula atual atualizada! 🎉" if valor else "Marcação de aula removida.", "sucesso")
            except Exception as e:
                flash(f"Erro ao salvar: {e}", "erro")
        return redirect(url_for("admin"))
    return render_template_string(PAGINA_ADMIN, css=BASE_CSS, aula_atual=obter_aula_atual())


@app.route("/")
@login_obrigatorio
def inicio():
    try:
        arquivos_base = drive_listar(DRIVE_PASTA_BASE)
    except Exception as e:
        arquivos_base = []
        flash(f"Não consegui listar os arquivos base: {e}", "erro")
    return render_template_string(
        PAGINA_INICIO,
        css=BASE_CSS,
        arquivos_base=arquivos_base,
        tipos_projeto=list(TIPOS_PROJETO.keys()),
        tipo_projeto_padrao=TIPO_PROJETO_PADRAO,
    )


@app.route("/enviar", methods=["POST"])
@login_obrigatorio
def enviar():
    nome_aluno = limpar_nome(request.form.get("nome", ""))
    arquivo = request.files.get("arquivo")
    tipo_projeto = request.form.get("tipo_projeto", TIPO_PROJETO_PADRAO)
    if tipo_projeto not in TIPOS_PROJETO:
        tipo_projeto = TIPO_PROJETO_PADRAO
    nome_subpasta = TIPOS_PROJETO[tipo_projeto]

    if not arquivo or arquivo.filename == "":
        flash("Você precisa escolher um arquivo.", "erro")
        return redirect(url_for("inicio"))

    ext = os.path.splitext(arquivo.filename)[1].lower()
    if ext not in EXTENSOES_OK:
        flash(f"Tipo de arquivo não permitido ({ext}).", "erro")
        return redirect(url_for("inicio"))

    # Nome final: NomeDoAluno_projeto_aula3_AAAAMMDD_HHMM.sb3 (horário de Brasília, GMT-3)
    # O "_aula3_" só aparece se o professor tiver definido a aula atual em /admin.
    aula_atual = obter_aula_atual()
    marca_aula = f"_aula{aula_atual}" if aula_atual else ""
    carimbo = datetime.datetime.now(TZ_BRASIL).strftime("%Y%m%d_%H%M")
    nome_final = f"{nome_aluno}_projeto{marca_aula}_{carimbo}{ext}"

    try:
        pasta_aluno_id = drive_obter_ou_criar_pasta(DRIVE_PASTA_UPLOADS, nome_aluno)
        pasta_tipo_id = drive_obter_ou_criar_pasta(pasta_aluno_id, nome_subpasta)
        dados = arquivo.read()
        drive_enviar(pasta_tipo_id, nome_final, dados, arquivo.mimetype or "application/octet-stream")
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
