"""
generate_backup_flash.py
Red de seguridad automática de EnergyFlash.
Réplica en Python de la lógica Apps Script del proyecto 'Informe Energia'.

Funciones replicadas:
  - obtenerDatosSheets()  ->  Lectura de Google Sheets (Esios-REE, commodities, Futuros)
  - llamarGemini()        ->  Llamada a la API de Gemini con Google Search grounding
  - publicarInformeWeb()  ->  Ensamblado del .md y push al repositorio de GitHub

Modo de ejecución:
  - Local:          python generate_backup_flash.py
  - GitHub Actions: via .github/workflows/publish_backup.yml (07:30 AM Madrid)

Variables de entorno requeridas (locales en .env o Secrets de GitHub):
  GEMINI_API_KEY       -> Clave API de Google Gemini
  GITHUB_TOKEN         -> Token de acceso al repo samuelgamito-Energy/energyflash-powertrader
  GOOGLE_CREDENTIALS   -> Contenido del JSON de cuenta de servicio de Google (para gspread)
"""

import os, sys, json, base64, datetime, requests

try:
    import gspread
    from google.oauth2.service_account import Credentials
except ImportError:
    print("FATAL: Faltan dependencias. Ejecuta: pip install gspread google-auth requests")
    sys.exit(1)

# ============================================================
# CONFIGURACION
# ============================================================
SPREADSHEET_ID = "1Rz4nfYQdp5SK63g_E8g-LpN0fSKlPCQKuSqxe_rF5eI"
REPO_OWNER     = "samuelgamito-Energy"
REPO_NAME      = "energyflash-powertrader"
BRANCH         = "main"
GEMINI_MODEL   = "gemini-2.5-flash"
GEMINI_API_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


# ============================================================
# 1. CONEXION A GOOGLE SHEETS
# ============================================================
def conectar_google_sheets():
    if not GOOGLE_CREDENTIALS:
        raise ValueError("GOOGLE_CREDENTIALS no definida.")
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds  = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(SPREADSHEET_ID)


# ============================================================
# 2. EXTRACCION DE DATOS (replica de obtenerDatosSheets())
# ============================================================
def obtener_datos_sheets(ss):
    txt_tec, txt_res, txt_comm, txt_fut = "", "", "", ""

    try:
        sg = ss.worksheet("Esios-REE")
        for row in sg.get("A1:B19"):
            if row and row[0]: txt_tec += " | ".join(str(c) for c in row) + "\n"
        for row in sg.get("A28:B28"):
            if row and row[0]: txt_res += " | ".join(str(c) for c in row) + "\n"
        print("OK: Esios-REE")
    except Exception as e:
        print(f"WARN: Esios-REE -> {e}")

    try:
        sc = ss.worksheet("commodities")
        for row in sc.get("A1:C11"):
            txt_comm += " | ".join(str(c) for c in row) + "\n"
        print("OK: commodities")
    except Exception as e:
        print(f"WARN: commodities -> {e}")

    try:
        sf = ss.worksheet("Futuros")
        txt_fut = "--- DATOS BRUTOS FUTUROS ---\n"
        for row in sf.get("A1:H20"):
            if row and row[0]: txt_fut += " | ".join(str(c) for c in row) + "\n"
        print("OK: Futuros")
    except Exception as e:
        print(f"WARN: Futuros no encontrado -> {e}")
        txt_fut = "ADVERTENCIA: No se encontro la pestana Futuros."

    return {"tecnologias": txt_tec, "resumen": txt_res, "commodities": txt_comm, "futuros": txt_fut}


def obtener_prompt_web(ss):
    cfg     = ss.worksheet("Config")
    p_web   = cfg.acell("A10").value or ""
    if not p_web:
        raise ValueError("Falta el prompt en Config!A10")
    return p_web


# ============================================================
# 3. LLAMADA A GEMINI (replica de llamarGemini())
# ============================================================
def llamar_gemini(prompt, bloque="BLOQUE"):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY no definida.")

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.3, "maxOutputTokens": 8192},
    }
    url  = f"{GEMINI_API_URL}?key={GEMINI_API_KEY}"
    resp = requests.post(url, json=payload, headers={"Content-Type": "application/json"}, timeout=120)
    resp.raise_for_status()
    data = resp.json()

    candidates = data.get("candidates", [])
    if candidates and candidates[0].get("content", {}).get("parts"):
        texto = "".join(p.get("text", "") for p in candidates[0]["content"]["parts"])
        print(f"OK: {bloque} ({len(texto)} chars)")
        return texto

    print(f"WARN: {bloque} devolvio respuesta vacia.")
    return "<p>La IA no devolvio texto para este bloque.</p>"


# ============================================================
# 4. GENERACION DEL CONTENIDO WEB
# ============================================================
def generar_contenido_web(prompt_web, datos, fecha_hoy):
    prompt_final = (
        prompt_web
        .replace("[FECHA_HOY]", fecha_hoy)
        .replace("[DATOS_GENERACION]", datos["tecnologias"])
        .replace("[DATOS_COMMODITIES]", datos["commodities"])
        .replace("[DATOS_FUTUROS]", datos["futuros"])
        .replace("[DATOS_RESUMEN]", datos["resumen"])
    )

    texto = llamar_gemini(prompt_final, "POST WEB")

    resumen, cuerpo = "", ""
    try:
        inicio = texto.index("{")
        fin    = texto.rindex("}") + 1
        datos_json = json.loads(texto[inicio:fin])
        resumen = datos_json.get("resumen", "")
        cuerpo  = datos_json.get("cuerpo", "")
    except (ValueError, json.JSONDecodeError):
        cuerpo  = texto.replace("```markdown", "").replace("```", "").strip()
        resumen = (cuerpo[:152] + "...") if len(cuerpo) > 155 else cuerpo

    return cuerpo, resumen


def ensamblar_markdown(cuerpo, resumen, fecha_hoy, fecha_iso):
    resumen_safe = resumen.replace('"', "'").replace("\n", " ")
    return (
        f'---\n'
        f'title: "Informe Energia: {fecha_hoy}"\n'
        f'date: {fecha_iso}\n'
        f'summary: "{resumen_safe}"\n'
        f'draft: false\n'
        f'categories: ["Informe Diario"]\n'
        f'tags: ["OMIE", "MIBGAS", "Mercados", "PowerTrader"]\n'
        f'author: "PowerTrader AI"\n'
        f'---\n\n'
        f'{cuerpo}\n\n'
        f'> **Fuente:** PowerTrader AI\n'
    )


# ============================================================
# 5. PUBLICACION EN GITHUB (replica de subirAGitHub())
# ============================================================
def subir_a_github(contenido, fecha_slug):
    if not GITHUB_TOKEN:
        raise ValueError("GITHUB_TOKEN no definida.")

    path    = f"content/posts/informe-{fecha_slug}.md"
    api_url = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Content-Type": "application/json"}

    sha = None
    try:
        r = requests.get(api_url, headers=headers, timeout=30)
        if r.status_code == 200:
            sha = r.json().get("sha")
            print(f"Archivo existente encontrado (SHA: {sha[:8]}...). Sobreescribiendo.")
    except Exception as e:
        print(f"WARN: No se pudo obtener SHA (puede ser nuevo): {e}")

    payload = {
        "message": f"Backup Auto-Publish {fecha_slug}",
        "content": base64.b64encode(contenido.encode("utf-8")).decode("utf-8"),
        "branch": BRANCH,
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(api_url, headers=headers, json=payload, timeout=30)
    if r.status_code in (200, 201):
        print(f"EXITO: Publicado -> {path} (HTTP {r.status_code})")
        return True
    elif r.status_code == 422:
        print(f"AVISO: El archivo ya existia sin cambios. HTTP 422")
        return True
    else:
        print(f"ERROR GitHub HTTP {r.status_code}: {r.text[:300]}")
        return False


# ============================================================
# MAIN
# ============================================================
def main():
    print("\n========================================")
    print("  EnergyFlash Backup Generator")
    print("========================================\n")

    now        = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=2)))
    fecha_hoy  = now.strftime("%d/%m/%Y")
    fecha_slug = now.strftime("%Y-%m-%d")
    fecha_iso  = now.isoformat()
    print(f"Fecha: {fecha_hoy}\n")

    print("Conectando a Google Sheets...")
    ss = conectar_google_sheets()
    print(f"Conectado a: {ss.title}\n")

    print("Extrayendo datos del mercado...")
    datos = obtener_datos_sheets(ss)
    print()

    print("Cargando prompt desde Config!A10...")
    prompt_web = obtener_prompt_web(ss)
    print("Prompt cargado.\n")

    print("Generando contenido con Gemini...")
    cuerpo, resumen = generar_contenido_web(prompt_web, datos, fecha_hoy)
    print()

    markdown_final = ensamblar_markdown(cuerpo, resumen, fecha_hoy, fecha_iso)

    output_path = os.path.join(
        os.path.dirname(__file__), "..", "push_temp", "content", "posts",
        f"informe-{fecha_slug}.md"
    )
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(markdown_final)
    print(f"Guardado localmente: {output_path}\n")

    print("Publicando en GitHub...")
    exito = subir_a_github(markdown_final, fecha_slug)

    if exito:
        print(f"\nEXITO: Informe del {fecha_hoy} publicado.")
        print(f"  -> https://energyflash.powertrader.es/posts/informe-{fecha_slug}/")
    else:
        print("\nFALLO: El informe no se publico.")
        sys.exit(1)


if __name__ == "__main__":
    main()
