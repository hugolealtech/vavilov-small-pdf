import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from flask import Flask, request, render_template_string, send_file, jsonify
from pypdf import PdfReader, PdfWriter
from werkzeug.utils import secure_filename

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
GS_TIMEOUT_SECONDS = 600
MAX_CONTENT_LENGTH = 1024 ** 3
SIZE_SAFETY_MARGIN = 0.97
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

# ATENÇÃO: limites variam por tribunal, sistema e portal, e mudam com o tempo.
# Conferir sempre na documentação oficial do destino antes de usar em produção.
PLATFORM_PRESETS = {
    'trf1': {
        'label': 'TRF1 (PJe)',
        'max_mb': 20,
        'note': 'PJe/TRF1: PDF até 20 MB por arquivo, até 40 arquivos por peticionamento.',
    },
    'tjdft': {
        'label': 'TJDFT (PJe)',
        'max_mb': 10,
        'note': 'PJe/TJDFT: PDF até 10 MB por arquivo.',
    },
    'gov': {
        'label': 'GOV',
        'max_mb': 5,
        'note': 'Valor provisório. Confirmar o limite do portal GOV utilizado.',
    },
}

_ACTIVE_TEMP_DIRS = set()
_ACTIVE_TEMP_DIRS_LOCK = threading.Lock()

QUALITY_LEVELS = {
    'screen': 'Máxima compressão (72 dpi, para tela)',
    'ebook':  'Alta compressão (150 dpi, recomendado)',
    'printer': 'Compressão moderada (300 dpi, para impressão)',
    'prepress': 'Compressão leve (alta qualidade)',
}

def compress_pdf(input_path, output_path, power='ebook'):
    gs_binary = shutil.which("gs")
    if not gs_binary:
        return "Ghostscript não encontrado. Instale-o ou reconstrua a imagem Docker."

    gs_command = [
        gs_binary, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{power}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={output_path}", input_path
    ]
    try:
        subprocess.run(
            gs_command, check=True, timeout=GS_TIMEOUT_SECONDS,
            capture_output=True, text=True
        )
    except subprocess.TimeoutExpired as e:
        print("Erro: timeout ao comprimir PDF")
        if e.stderr:
            print(e.stderr.strip())
        return "A compressão excedeu o limite de 10 minutos. Tente dividir o PDF em arquivos menores."
    except subprocess.CalledProcessError as e:
        details = (e.stderr or e.stdout or str(e)).strip()
        print(f"Erro do Ghostscript ao comprimir PDF: {details}")
        return "O Ghostscript não conseguiu processar este PDF. Confira os logs do servidor."
    return None

def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 ** 2):.2f} MB"


def cleanup_old_files(max_age_seconds=3600):
    """Remove arquivos e diretórios de trabalho temporários com mais de uma hora."""
    cutoff = time.time() - max_age_seconds
    with _ACTIVE_TEMP_DIRS_LOCK:
        active_dirs = set(_ACTIVE_TEMP_DIRS)
    for entry in os.scandir(UPLOAD_FOLDER):
        if entry.path in active_dirs:
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                os.remove(entry.path)
        except FileNotFoundError:
            continue


def create_temp_dir():
    cleanup_old_files()
    path = tempfile.mkdtemp(prefix='_tmp_', dir=UPLOAD_FOLDER)
    with _ACTIVE_TEMP_DIRS_LOCK:
        _ACTIVE_TEMP_DIRS.add(path)
    return path


def validate_pdf(path):
    """Valida o PDF e rejeita arquivos criptografados ou sem páginas."""
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            raise ValueError('PDF criptografado. Remova a senha antes de enviar o arquivo.')
        page_count = len(reader.pages)
        if page_count == 0:
            raise ValueError('O PDF não contém páginas.')
        return reader, page_count
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError('PDF inválido ou corrompido. Confira o arquivo e tente novamente.') from exc


def parse_max_size_bytes(raw_value):
    if raw_value in (None, ''):
        return None
    try:
        size_bytes = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError('Tamanho máximo inválido. Informe um número inteiro de bytes.') from exc
    if size_bytes <= 0:
        raise ValueError('O tamanho máximo deve ser maior que zero.')
    return size_bytes


def write_page_range(reader, start, end, output_path):
    """Grava as páginas no intervalo semiaberto [start, end)."""
    writer = PdfWriter()
    for page_number in range(start, end):
        writer.add_page(reader.pages[page_number])
    with open(output_path, 'wb') as output_file:
        writer.write(output_file)
    return os.path.getsize(output_path)


def parse_page_ranges(raw_ranges, page_count):
    ranges = []
    for item in raw_ranges.split(','):
        match = re.fullmatch(r'\s*(\d+)\s*-\s*(\d+)\s*', item)
        if not match:
            raise ValueError('Intervalo inválido. Use o formato, por exemplo, 1-5, 6-10.')
        first, last = map(int, match.groups())
        if first < 1 or last < first or last > page_count:
            raise ValueError(f'Intervalo {first}-{last} inválido. O PDF tem {page_count} páginas.')
        if any(first <= existing_last and last >= existing_first
               for existing_first, existing_last in ranges):
            raise ValueError('Os intervalos de páginas não podem se sobrepor.')
        ranges.append((first, last))
    if not ranges:
        raise ValueError('Informe ao menos um intervalo de páginas.')
    return ranges


def find_page_ranges_by_size(
    reader, page_count, base_size, target_size, work_dir,
    compress_parts=True, quality='ebook'
):
    """Monta partes sequenciais e ajusta cada parte pelo tamanho real do PDF."""
    parts = []
    start = 0
    average_page_size = max(1, base_size // page_count)
    estimated_pages = max(1, int(target_size / average_page_size))

    while start < page_count:
        end = min(page_count, start + estimated_pages)
        raw_path = os.path.join(work_dir, f'raw_{uuid.uuid4().hex}.pdf')

        # A busca binária evita serializar individualmente cada página.
        low, high = start + 1, end
        best_end = start
        while low <= high:
            middle = (low + high) // 2
            raw_size = write_page_range(reader, start, middle, raw_path)
            if raw_size <= target_size or middle == start + 1:
                best_end = middle
                low = middle + 1
            else:
                high = middle - 1

        if best_end <= start:
            best_end = start + 1
        end = best_end

        while True:
            raw_size = write_page_range(reader, start, end, raw_path)
            warnings = []
            if not compress_parts:
                chosen_path = raw_path
                chosen_size = raw_size
            else:
                compressed_path = os.path.join(work_dir, f'compressed_{uuid.uuid4().hex}.pdf')
                compression_error = compress_pdf(raw_path, compressed_path, power=quality)
                if compression_error:
                    chosen_path = raw_path
                    chosen_size = raw_size
                    warnings.append(
                        f'Parte temporária (páginas {start + 1}-{end}): '
                        f'falha ao comprimir; usada sem compressão ({compression_error})'
                    )
                else:
                    compressed_size = os.path.getsize(compressed_path)
                    chosen_path = compressed_path if compressed_size < raw_size else raw_path
                    chosen_size = min(compressed_size, raw_size)

            if chosen_size <= target_size or end == start + 1:
                if compress_parts and chosen_size > target_size and end == start + 1:
                    screen_path = os.path.join(work_dir, f'screen_{uuid.uuid4().hex}.pdf')
                    screen_error = compress_pdf(raw_path, screen_path, power='screen')
                    if not screen_error:
                        screen_size = os.path.getsize(screen_path)
                        if screen_size < chosen_size:
                            chosen_path, chosen_size = screen_path, screen_size
                if chosen_size > target_size:
                    warnings.append(
                        f'Páginas {start + 1}-{end}: o tamanho da parte excede o limite.'
                    )
                parts.append({
                    'path': chosen_path,
                    'start': start + 1,
                    'end': end,
                    'size_bytes': chosen_size,
                    'warnings': warnings,
                })
                start = end
                break

            # Reduz o lote e repete a medição antes de fechar a parte.
            end = start + max(1, (end - start) // 2)

    return parts


def make_manual_parts(reader, ranges, work_dir, compress_parts, quality):
    parts = []
    for start, end in ranges:
        raw_path = os.path.join(work_dir, f'raw_{uuid.uuid4().hex}.pdf')
        raw_size = write_page_range(reader, start - 1, end, raw_path)
        chosen_path = raw_path
        chosen_size = raw_size
        warnings = []
        if compress_parts:
            compressed_path = os.path.join(work_dir, f'compressed_{uuid.uuid4().hex}.pdf')
            compression_error = compress_pdf(raw_path, compressed_path, power=quality)
            if compression_error:
                warnings.append(
                    f'Páginas {start}-{end}: falha ao comprimir; '
                    f'usada sem compressão ({compression_error})'
                )
            else:
                compressed_size = os.path.getsize(compressed_path)
                if compressed_size < raw_size:
                    chosen_path, chosen_size = compressed_path, compressed_size
        parts.append({
            'path': chosen_path,
            'start': start,
            'end': end,
            'size_bytes': chosen_size,
            'warnings': warnings,
        })
    return parts


def create_parts_zip(parts, original_name, work_dir):
    part_count = len(parts)
    base_name = os.path.splitext(original_name)[0] or 'documento'
    zip_id = uuid.uuid4().hex
    zip_name = f'vavilov_{base_name}_partes_{zip_id}.zip'
    zip_path = os.path.join(work_dir, f'{zip_id}.zip')
    final_zip_path = os.path.join(UPLOAD_FOLDER, zip_name)
    part_details = []

    with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_STORED) as archive:
        for index, part in enumerate(parts, start=1):
            part_name = (
                f'vavilov_{base_name}_parte_{index:02d}_de_{part_count:02d}.pdf'
            )
            archive.write(part['path'], arcname=part_name)
            part_details.append({
                'name': part_name,
                'pages': f"{part['start']}-{part['end']}",
                'size': format_size(part['size_bytes']),
            })

    os.replace(zip_path, final_zip_path)
    return zip_name, part_details

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>Vavilov · Compressor de PDF</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;700;800&family=DM+Mono:wght@300;400&display=swap" rel="stylesheet">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    :root {
      --bg: #0d0d0d;
      --surface: #161616;
      --border: #2a2a2a;
      --accent: #c8f135;
      --accent-dim: #a8cc22;
      --text: #f0f0f0;
      --muted: #666;
      --danger: #ff5a5a;
      --success: #c8f135;
    }

    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'DM Mono', monospace;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem 1rem;
    }

    .grain {
      position: fixed; inset: 0; pointer-events: none; z-index: 0;
      background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.04'/%3E%3C/svg%3E");
      background-size: 150px;
      opacity: 0.5;
    }

    .container {
      position: relative; z-index: 1;
      width: 100%; max-width: 560px;
    }

    header {
      margin-bottom: 2.5rem;
    }

    .logo {
      font-family: 'Syne', sans-serif;
      font-weight: 800;
      font-size: 2.2rem;
      letter-spacing: -0.04em;
      color: var(--text);
      display: flex;
      align-items: baseline;
      gap: 0.3rem;
    }

    .logo span {
      color: var(--accent);
      font-size: 2.6rem;
    }

    .tagline {
      font-size: 0.72rem;
      color: var(--muted);
      letter-spacing: 0.15em;
      text-transform: uppercase;
      margin-top: 0.25rem;
    }

    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 2rem;
    }

    /* Drop zone */
    .drop-zone {
      border: 2px dashed var(--border);
      border-radius: 8px;
      padding: 2.5rem 1.5rem;
      text-align: center;
      cursor: pointer;
      transition: border-color 0.2s, background 0.2s;
      position: relative;
    }
    .drop-zone:hover, .drop-zone.drag-over {
      border-color: var(--accent);
      background: rgba(200,241,53,0.04);
    }
    .drop-zone input[type="file"] {
      position: absolute; inset: 0; opacity: 0; cursor: pointer; width: 100%; height: 100%;
    }
    .drop-icon {
      font-size: 2.2rem;
      margin-bottom: 0.75rem;
      display: block;
    }
    .drop-label {
      font-family: 'Syne', sans-serif;
      font-weight: 700;
      font-size: 1rem;
      color: var(--text);
      margin-bottom: 0.3rem;
    }
    .drop-sub {
      font-size: 0.72rem;
      color: var(--muted);
    }
    .file-selected {
      margin-top: 0.75rem;
      font-size: 0.78rem;
      color: var(--accent);
      display: none;
    }

    /* Quality */
    .field-group {
      margin-top: 1.5rem;
    }
    .field-help, .preset-note {
      display: block;
      margin-top: 0.45rem;
      color: var(--muted);
      font-size: 0.68rem;
      line-height: 1.5;
    }
    .size-controls, .preset-chips, .mode-controls {
      display: flex;
      gap: 0.5rem;
      align-items: center;
    }
    .size-controls input, .size-controls select, .mode-controls input,
    .mode-controls select, .mode-controls textarea {
      min-width: 0;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.65rem 0.75rem;
      background: #101010;
      color: var(--text);
      font: inherit;
      font-size: 0.78rem;
    }
    .size-controls input { flex: 1; }
    .preset-chips { flex-wrap: wrap; margin-top: 0.65rem; }
    .preset-chip, .tab-button {
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 0.45rem 0.8rem;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-size: 0.7rem;
    }
    .preset-chip:hover, .preset-chip.active, .tab-button.active {
      border-color: var(--accent);
      color: var(--accent);
      background: rgba(200,241,53,0.07);
    }
    .tab-row {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 0.5rem;
    }
    .tab-button { border-radius: 6px; flex: 1; }
    .mode-panel { display: none; }
    .mode-panel.active { display: block; }
    .mode-controls { flex-wrap: wrap; }
    .mode-controls input[type="number"] { width: 120px; }
    .mode-controls input[type="text"], .mode-controls textarea { flex: 1; }
    .check-row {
      display: flex;
      align-items: center;
      gap: 0.55rem;
      margin-top: 1rem;
      color: var(--muted);
      font-size: 0.74rem;
    }
    .check-row input { accent-color: var(--accent); }
    label.field-label {
      display: block;
      font-size: 0.7rem;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 0.6rem;
    }
    .quality-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.5rem;
    }
    .quality-opt {
      position: relative;
    }
    .quality-opt input { display: none; }
    .quality-opt label {
      display: block;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 0.6rem 0.75rem;
      cursor: pointer;
      font-size: 0.72rem;
      line-height: 1.4;
      color: var(--muted);
      transition: all 0.15s;
    }
    .quality-opt input:checked + label {
      border-color: var(--accent);
      color: var(--accent);
      background: rgba(200,241,53,0.07);
    }
    .quality-opt label:hover {
      border-color: #444;
      color: var(--text);
    }
    .q-name {
      display: block;
      font-family: 'Syne', sans-serif;
      font-weight: 700;
      font-size: 0.8rem;
      color: inherit;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 0.15rem;
    }

    /* Button */
    .btn {
      margin-top: 1.5rem;
      width: 100%;
      background: var(--accent);
      color: #0d0d0d;
      border: none;
      border-radius: 6px;
      padding: 0.9rem;
      font-family: 'Syne', sans-serif;
      font-weight: 800;
      font-size: 0.95rem;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      cursor: pointer;
      transition: background 0.15s, transform 0.1s;
    }
    .btn:hover { background: var(--accent-dim); }
    .btn:active { transform: scale(0.98); }
    .btn:disabled { background: #333; color: #666; cursor: not-allowed; }

    /* Result */
    .result {
      display: none;
      margin-top: 1.5rem;
      border-radius: 8px;
      padding: 1rem 1.25rem;
      font-size: 0.8rem;
      line-height: 1.6;
    }
    .result.success {
      background: rgba(200,241,53,0.08);
      border: 1px solid rgba(200,241,53,0.3);
      color: var(--accent);
    }
    .result.warning {
      background: rgba(255,200,50,0.08);
      border: 1px solid rgba(255,200,50,0.3);
      color: #ffd060;
    }
    .result.error {
      background: rgba(255,90,90,0.08);
      border: 1px solid rgba(255,90,90,0.3);
      color: var(--danger);
    }
    .result-title {
      font-family: 'Syne', sans-serif;
      font-weight: 700;
      font-size: 0.9rem;
      margin-bottom: 0.3rem;
    }
    .result a {
      color: inherit;
      font-weight: 700;
      text-decoration: underline;
      text-underline-offset: 3px;
    }

    .spinner {
      display: none;
      margin: 0.5rem auto 0;
      width: 20px; height: 20px;
      border: 2px solid #333;
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
    }
    .spinner-label {
      display: none;
      margin-top: 0.5rem;
      color: var(--muted);
      text-align: center;
      font-size: 0.7rem;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    footer {
      margin-top: 2rem;
      font-size: 0.65rem;
      color: var(--muted);
      text-align: center;
      letter-spacing: 0.08em;
    }
  </style>
</head>
<body>
  <div class="grain"></div>
  <div class="container">
    <header>
      <div class="logo">vavilov<span>.</span></div>
      <div class="tagline">compressor de PDF · sem mistério</div>
    </header>

    <div class="card">
      <div class="tab-row">
        <button class="tab-button active" type="button" data-tab="compress">Comprimir PDF</button>
        <button class="tab-button" type="button" data-tab="split">Dividir PDF</button>
      </div>
      <div class="drop-zone" id="dropZone">
        <input type="file" id="pdfFile" accept=".pdf,application/pdf">
        <span class="drop-icon">📄</span>
        <div class="drop-label">Arraste o PDF aqui</div>
        <div class="drop-sub">ou clique para selecionar</div>
        <div class="file-selected" id="fileSelected"></div>
      </div>

      <div class="field-group">
        <label class="field-label">Nível de compressão</label>
        <div class="quality-grid">
          <div class="quality-opt">
            <input type="radio" name="quality" id="q-screen" value="screen">
            <label for="q-screen"><span class="q-name">Máxima</span>72 dpi · para tela</label>
          </div>
          <div class="quality-opt">
            <input type="radio" name="quality" id="q-ebook" value="ebook" checked>
            <label for="q-ebook"><span class="q-name">Alta ✦</span>150 dpi · recomendado</label>
          </div>
          <div class="quality-opt">
            <input type="radio" name="quality" id="q-printer" value="printer">
            <label for="q-printer"><span class="q-name">Moderada</span>300 dpi · impressão</label>
          </div>
          <div class="quality-opt">
            <input type="radio" name="quality" id="q-prepress" value="prepress">
            <label for="q-prepress"><span class="q-name">Leve</span>alta qualidade</label>
          </div>
        </div>
      </div>

      <div class="field-group">
        <label class="field-label" for="maxSize">Tamanho máximo por arquivo</label>
        <div class="size-controls">
          <input type="number" id="maxSize" min="0" step="any" inputmode="decimal"
                 aria-label="Tamanho máximo por arquivo">
          <select id="sizeUnit" aria-label="Unidade do tamanho máximo">
            <option value="MB">MB</option>
            <option value="KB">KB</option>
          </select>
        </div>
        <span class="field-help">Deixe em branco para não dividir. Se o PDF comprimido passar desse limite, ele será dividido automaticamente e você receberá um .zip.</span>
        <div class="preset-chips" id="presetChips"></div>
        <span class="preset-note" id="presetNote" aria-live="polite"></span>
      </div>

      <div class="field-group mode-panel" id="splitOptions">
        <label class="field-label" for="splitMode">Modo de divisão</label>
        <select id="splitMode" class="mode-controls">
          <option value="size">Por tamanho máximo</option>
          <option value="pages">A cada N páginas</option>
          <option value="ranges">Por intervalos de páginas</option>
        </select>
        <div class="field-group" id="pagesOption" style="display:none">
          <label class="field-label" for="pagesPerPart">Páginas por parte</label>
          <input type="number" id="pagesPerPart" min="1" step="1" value="10">
        </div>
        <div class="field-group" id="rangesOption" style="display:none">
          <label class="field-label" for="pageRanges">Intervalos (ex.: 1-5, 6-10, 11-20)</label>
          <input type="text" id="pageRanges" placeholder="1-5, 6-10, 11-20">
        </div>
        <label class="check-row">
          <input type="checkbox" id="compressParts" checked>
          Comprimir também as partes
        </label>
      </div>

      <button class="btn" id="compressBtn" disabled>Comprimir PDF</button>
      <button class="btn" id="splitBtn" style="display:none" disabled>Dividir PDF</button>
      <div class="spinner" id="spinner"></div>
      <div class="spinner-label" id="spinnerLabel">Comprimindo e dividindo, isso pode levar alguns minutos...</div>

      <div class="result" id="resultBox">
        <div class="result-title" id="resultTitle"></div>
        <div id="resultBody"></div>
      </div>
    </div>

    <footer>processado localmente no servidor · seus arquivos não são armazenados</footer>
  </div>

  <script>
    const platformPresets = {{ presets|tojson }};
    const fileInput = document.getElementById('pdfFile');
    const dropZone = document.getElementById('dropZone');
    const fileSelected = document.getElementById('fileSelected');
    const compressBtn = document.getElementById('compressBtn');
    const splitBtn = document.getElementById('splitBtn');
    const spinner = document.getElementById('spinner');
    const spinnerLabel = document.getElementById('spinnerLabel');
    const resultBox = document.getElementById('resultBox');
    const resultTitle = document.getElementById('resultTitle');
    const resultBody = document.getElementById('resultBody');
    const maxSizeInput = document.getElementById('maxSize');
    const sizeUnit = document.getElementById('sizeUnit');
    const presetNote = document.getElementById('presetNote');
    const splitMode = document.getElementById('splitMode');
    let activePreset = null;
    let activeTab = 'compress';

    Object.entries(platformPresets).forEach(([key, preset]) => {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'preset-chip';
      chip.textContent = preset.label;
      chip.dataset.preset = key;
      chip.addEventListener('click', () => {
        if (activePreset === key) {
          maxSizeInput.value = '';
          activePreset = null;
          presetNote.textContent = '';
          chip.classList.remove('active');
          return;
        }
        document.querySelectorAll('.preset-chip').forEach(item => item.classList.remove('active'));
        activePreset = key;
        maxSizeInput.value = preset.max_mb * (sizeUnit.value === 'MB' ? 1 : 1024);
        chip.classList.add('active');
        presetNote.textContent = preset.note;
      });
      document.getElementById('presetChips').appendChild(chip);
    });

    maxSizeInput.addEventListener('input', () => {
      if (activePreset) {
        activePreset = null;
        document.querySelectorAll('.preset-chip').forEach(item => item.classList.remove('active'));
        presetNote.textContent = '';
      }
    });
    sizeUnit.addEventListener('change', () => {
      if (activePreset) {
        activePreset = null;
        document.querySelectorAll('.preset-chip').forEach(item => item.classList.remove('active'));
        presetNote.textContent = '';
      }
    });

    document.querySelectorAll('.tab-button').forEach(button => {
      button.addEventListener('click', () => {
        activeTab = button.dataset.tab;
        document.querySelectorAll('.tab-button').forEach(item => item.classList.toggle('active', item === button));
        document.getElementById('splitOptions').classList.toggle('active', activeTab === 'split');
        compressBtn.style.display = activeTab === 'compress' ? 'block' : 'none';
        splitBtn.style.display = activeTab === 'split' ? 'block' : 'none';
      });
    });

    splitMode.addEventListener('change', () => {
      document.getElementById('pagesOption').style.display = splitMode.value === 'pages' ? 'block' : 'none';
      document.getElementById('rangesOption').style.display = splitMode.value === 'ranges' ? 'block' : 'none';
    });

    function updateSelectedFile(file) {
      if (!file || !file.name.toLowerCase().endsWith('.pdf')) {
        fileSelected.textContent = 'Selecione um arquivo PDF válido.';
        fileSelected.style.display = 'block';
        compressBtn.disabled = true;
        splitBtn.disabled = true;
        return;
      }
      fileSelected.textContent = `✔ ${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
      fileSelected.style.display = 'block';
      compressBtn.disabled = false;
      splitBtn.disabled = false;
    }

    fileInput.addEventListener('change', () => {
      updateSelectedFile(fileInput.files[0]);
    });

    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
      const f = e.dataTransfer.files[0];
      if (f && f.name.toLowerCase().endsWith('.pdf')) {
        fileInput.files = e.dataTransfer.files;
        updateSelectedFile(f);
      }
    });

    async function submitPdf(endpoint) {
      const file = fileInput.files[0];
      const quality = document.querySelector('input[name="quality"]:checked').value;
      if (!file) return;

      compressBtn.disabled = true;
      splitBtn.disabled = true;
      spinner.style.display = 'block';
      spinnerLabel.style.display = 'block';
      resultBox.style.display = 'none';

      const formData = new FormData();
      formData.append('pdf_file', file);
      formData.append('quality', quality);
      const sizeValue = Number(maxSizeInput.value);
      const unitMultiplier = sizeUnit.value === 'MB' ? 1024 ** 2 : 1024;
      const maxSizeBytes = maxSizeInput.value.trim() ? Math.floor(sizeValue * unitMultiplier) : null;
      if (maxSizeBytes !== null) formData.append('max_size_bytes', String(maxSizeBytes));
      if (activePreset) formData.append('preset', activePreset);

      if (endpoint === '/split') {
        formData.append('mode', splitMode.value);
        formData.append('compress_parts', String(document.getElementById('compressParts').checked));
        formData.append('pages_per_part', document.getElementById('pagesPerPart').value);
        formData.append('page_ranges', document.getElementById('pageRanges').value);
      }

      try {
        const resp = await fetch(endpoint, { method: 'POST', body: formData });
        const data = await resp.json();

        spinner.style.display = 'none';
        spinnerLabel.style.display = 'none';
        resultBox.style.display = 'block';

        if (data.status === 'ok_zip') {
          resultBox.className = 'result success';
          resultTitle.textContent = endpoint === '/compress'
            ? '✔ PDF comprimido e dividido com sucesso!'
            : '✔ PDF dividido com sucesso!';
          const partsMarkup = data.parts.map(part =>
            `<li><strong>${part.name}</strong> · páginas ${part.pages} · ${part.size}</li>`
          ).join('');
          const warningsMarkup = (data.warnings || []).map(warning =>
            `<li>${warning}</li>`
          ).join('');
          resultBody.innerHTML = `
            Tamanho original: <strong>${data.original_size}</strong><br>
            Tamanho total das partes: <strong>${data.total_compressed_size}</strong>
            <ul>${partsMarkup}</ul>
            ${warningsMarkup ? `<strong>Avisos</strong><ul>${warningsMarkup}</ul>` : ''}
            <a href="${data.download_url}" download>⬇ Baixar .zip</a>
          `;
        } else if (data.status === 'ok') {
          resultBox.className = 'result success';
          resultTitle.textContent = '✔ Comprimido com sucesso!';
          resultBody.innerHTML = `
            Tamanho original: <strong>${data.original_size}</strong><br>
            Tamanho final: <strong>${data.compressed_size}</strong><br>
            Redução: <strong>${data.reduction}</strong><br><br>
            <a href="${data.download_url}" download>⬇ Baixar PDF comprimido</a>
          `;
        } else if (data.status === 'already_small') {
          resultBox.className = 'result warning';
          resultTitle.textContent = '⚠ Arquivo já está pequeno o suficiente';
          resultBody.innerHTML = `
            O PDF comprimido (${data.compressed_size}) ficou maior ou igual ao original (${data.original_size}).<br>
            Nenhuma compressão foi aplicada — o arquivo original já está no tamanho ideal para este nível de qualidade.
          `;
        } else {
          resultBox.className = 'result error';
          resultTitle.textContent = activeTab === 'split' ? '✖ Erro ao dividir' : '✖ Erro ao comprimir';
          resultBody.textContent = data.message || 'Ocorreu um erro inesperado.';
        }
      } catch (err) {
        spinner.style.display = 'none';
        spinnerLabel.style.display = 'none';
        resultBox.style.display = 'block';
        resultBox.className = 'result error';
        resultTitle.textContent = '✖ Erro de conexão';
        resultBody.textContent = 'Não foi possível conectar ao servidor.';
      }

      compressBtn.disabled = !fileInput.files[0];
      splitBtn.disabled = !fileInput.files[0];
    }

    compressBtn.addEventListener('click', () => submitPdf('/compress'));
    splitBtn.addEventListener('click', () => submitPdf('/split'));
  </script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE, presets=PLATFORM_PRESETS)


@app.errorhandler(413)
def request_too_large(_error):
    return jsonify({
        'status': 'error',
        'message': 'O arquivo excede o limite de envio de 1 GB.',
    }), 413


@app.route('/compress', methods=['POST'])
def compress():
    file = request.files.get('pdf_file')
    quality = request.form.get('quality', 'ebook')

    if quality not in QUALITY_LEVELS:
        quality = 'ebook'

    if not file or not file.filename or not file.filename.lower().endswith('.pdf'):
        return jsonify({'status': 'error', 'message': 'Por favor, envie um arquivo PDF válido.'}), 400

    try:
        max_size_bytes = parse_max_size_bytes(request.form.get('max_size_bytes'))
    except ValueError as exc:
        return jsonify({'status': 'error', 'message': str(exc)}), 400

    name = secure_filename(file.filename)
    if not name:
        return jsonify({'status': 'error', 'message': 'Nome de arquivo inválido.'}), 400
    work_dir = create_temp_dir()
    request_id = uuid.uuid4().hex
    in_p = os.path.join(work_dir, f'in_{request_id}_{name}')
    out_p = os.path.join(work_dir, f'out_{request_id}_{name}')
    try:
        file.save(in_p)
        original_size = os.path.getsize(in_p)
        try:
            validate_pdf(in_p)
        except ValueError as exc:
            return jsonify({'status': 'error', 'message': str(exc)}), 400

        compression_error = compress_pdf(in_p, out_p, power=quality)
        if compression_error:
            return jsonify({'status': 'error', 'message': compression_error}), 500

        compressed_size = os.path.getsize(out_p)
        compressed_reduced = compressed_size < original_size

        # Sem limite informado, mantém o resultado e as mensagens do fluxo original.
        if max_size_bytes is None and not compressed_reduced:
            os.remove(out_p)
            return jsonify({
                'status': 'already_small',
                'original_size': format_size(original_size),
                'compressed_size': format_size(compressed_size),
            })

        target_size = max(1, int(max_size_bytes * SIZE_SAFETY_MARGIN)) if max_size_bytes else None
        if target_size is not None:
            base_path = out_p if compressed_reduced else in_p
            base_size = compressed_size if compressed_reduced else original_size
            if base_size > target_size:
                try:
                    base_reader, base_page_count = validate_pdf(base_path)
                    parts = find_page_ranges_by_size(
                        base_reader, base_page_count, base_size, target_size, work_dir,
                        compress_parts=True, quality=quality
                    )
                    zip_name, part_details = create_parts_zip(parts, name, work_dir)
                except ValueError as exc:
                    return jsonify({'status': 'error', 'message': str(exc)}), 400

                warnings = [
                    warning for part in parts for warning in part['warnings']
                ]
                return jsonify({
                    'status': 'ok_zip',
                    'original_size': format_size(original_size),
                    'total_compressed_size': format_size(
                        sum(part['size_bytes'] for part in parts)
                    ),
                    'parts': part_details,
                    'warnings': warnings,
                    'download_url': f'/download/{zip_name}',
                })

        result_path = out_p if compressed_reduced else in_p
        result_size = compressed_size if compressed_reduced else original_size
        reduction_pct = (1 - result_size / original_size) * 100
        download_name = f'vavilov_{name}_{request_id}.pdf'

        final_path = os.path.join(UPLOAD_FOLDER, download_name)
        shutil.copyfile(result_path, final_path)

        return jsonify({
            'status': 'ok',
            'original_size': format_size(original_size),
            'compressed_size': format_size(result_size),
            'reduction': f"{reduction_pct:.1f}%",
            'download_url': f'/download/{download_name}',
        })

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        with _ACTIVE_TEMP_DIRS_LOCK:
            _ACTIVE_TEMP_DIRS.discard(work_dir)


@app.route('/split', methods=['POST'])
def split():
    file = request.files.get('pdf_file')
    if not file or not file.filename or not file.filename.lower().endswith('.pdf'):
        return jsonify({'status': 'error', 'message': 'Por favor, envie um arquivo PDF válido.'}), 400

    name = secure_filename(file.filename)
    if not name:
        return jsonify({'status': 'error', 'message': 'Nome de arquivo inválido.'}), 400
    quality = request.form.get('quality', 'ebook')
    if quality not in QUALITY_LEVELS:
        quality = 'ebook'
    mode = request.form.get('mode', 'size')
    if mode not in {'size', 'pages', 'ranges'}:
        return jsonify({'status': 'error', 'message': 'Modo de divisão inválido.'}), 400
    compress_parts = request.form.get('compress_parts', 'true').lower() in {
        'true', '1', 'yes', 'on'
    }
    work_dir = create_temp_dir()
    in_p = os.path.join(work_dir, f'in_{uuid.uuid4().hex}_{name}')
    try:
        file.save(in_p)
        original_size = os.path.getsize(in_p)
        try:
            reader, page_count = validate_pdf(in_p)
            if mode == 'size':
                max_size_bytes = parse_max_size_bytes(request.form.get('max_size_bytes'))
                if max_size_bytes is None:
                    raise ValueError('Informe o tamanho máximo por arquivo para dividir por tamanho.')
                target_size = max(1, int(max_size_bytes * SIZE_SAFETY_MARGIN))
                parts = find_page_ranges_by_size(
                    reader, page_count, original_size, target_size, work_dir,
                    compress_parts=compress_parts, quality=quality
                )
            elif mode == 'pages':
                try:
                    pages_per_part = int(request.form.get('pages_per_part', ''))
                except (TypeError, ValueError) as exc:
                    raise ValueError('Informe um número válido de páginas por parte.') from exc
                if pages_per_part <= 0:
                    raise ValueError('O número de páginas por parte deve ser maior que zero.')
                ranges = [
                    (start + 1, min(page_count, start + pages_per_part))
                    for start in range(0, page_count, pages_per_part)
                ]
                parts = make_manual_parts(
                    reader, ranges, work_dir, compress_parts, quality
                )
            else:
                ranges = parse_page_ranges(
                    request.form.get('page_ranges', ''), page_count
                )
                parts = make_manual_parts(
                    reader, ranges, work_dir, compress_parts, quality
                )
            zip_name, part_details = create_parts_zip(parts, name, work_dir)
        except ValueError as exc:
            return jsonify({'status': 'error', 'message': str(exc)}), 400

        warnings = [warning for part in parts for warning in part['warnings']]
        return jsonify({
            'status': 'ok_zip',
            'original_size': format_size(original_size),
            'total_compressed_size': format_size(
                sum(part['size_bytes'] for part in parts)
            ),
            'parts': part_details,
            'warnings': warnings,
            'download_url': f'/download/{zip_name}',
        })
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        with _ACTIVE_TEMP_DIRS_LOCK:
            _ACTIVE_TEMP_DIRS.discard(work_dir)

@app.route('/download/<filename>')
def download(filename):
    # Segurança: apenas arquivos dentro de UPLOAD_FOLDER e prefixo vavilov_
    if (
        not filename.startswith('vavilov_')
        or '/' in filename
        or '..' in filename
        or filename != secure_filename(filename)
        or not filename.lower().endswith(('.pdf', '.zip'))
    ):
        return jsonify({'status': 'error', 'message': 'Arquivo não encontrado.'}), 404
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({'status': 'error', 'message': 'Arquivo não encontrado ou expirado.'}), 404
    return send_file(path, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095)
