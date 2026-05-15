import os
import subprocess
from flask import Flask, request, render_template_string, send_file, jsonify

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

QUALITY_LEVELS = {
    'screen': 'Máxima compressão (72 dpi, para tela)',
    'ebook':  'Alta compressão (150 dpi, recomendado)',
    'printer': 'Compressão moderada (300 dpi, para impressão)',
    'prepress': 'Compressão leve (alta qualidade)',
}

def compress_pdf(input_path, output_path, power='ebook'):
    gs_command = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{power}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={output_path}", input_path
    ]
    try:
        subprocess.run(gs_command, check=True, timeout=120)
        return True
    except subprocess.TimeoutExpired:
        print("Erro: timeout ao comprimir PDF")
        return False
    except Exception as e:
        print(f"Erro: {e}")
        return False

def format_size(size_bytes):
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 ** 2):.2f} MB"

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
      <div class="drop-zone" id="dropZone">
        <input type="file" id="pdfFile" accept=".pdf">
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

      <button class="btn" id="compressBtn" disabled>Comprimir PDF</button>
      <div class="spinner" id="spinner"></div>

      <div class="result" id="resultBox">
        <div class="result-title" id="resultTitle"></div>
        <div id="resultBody"></div>
      </div>
    </div>

    <footer>processado localmente no servidor · seus arquivos não são armazenados</footer>
  </div>

  <script>
    const fileInput = document.getElementById('pdfFile');
    const dropZone = document.getElementById('dropZone');
    const fileSelected = document.getElementById('fileSelected');
    const compressBtn = document.getElementById('compressBtn');
    const spinner = document.getElementById('spinner');
    const resultBox = document.getElementById('resultBox');
    const resultTitle = document.getElementById('resultTitle');
    const resultBody = document.getElementById('resultBody');

    fileInput.addEventListener('change', () => {
      const f = fileInput.files[0];
      if (f) {
        fileSelected.textContent = `✔ ${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
        fileSelected.style.display = 'block';
        compressBtn.disabled = false;
      }
    });

    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', e => {
      e.preventDefault();
      dropZone.classList.remove('drag-over');
      const f = e.dataTransfer.files[0];
      if (f && f.name.endsWith('.pdf')) {
        fileInput.files = e.dataTransfer.files;
        fileSelected.textContent = `✔ ${f.name} (${(f.size / 1024).toFixed(1)} KB)`;
        fileSelected.style.display = 'block';
        compressBtn.disabled = false;
      }
    });

    compressBtn.addEventListener('click', async () => {
      const file = fileInput.files[0];
      const quality = document.querySelector('input[name="quality"]:checked').value;
      if (!file) return;

      compressBtn.disabled = true;
      spinner.style.display = 'block';
      resultBox.style.display = 'none';

      const formData = new FormData();
      formData.append('pdf_file', file);
      formData.append('quality', quality);

      try {
        const resp = await fetch('/compress', { method: 'POST', body: formData });
        const data = await resp.json();

        spinner.style.display = 'none';
        resultBox.style.display = 'block';

        if (data.status === 'ok') {
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
          resultTitle.textContent = '✖ Erro ao comprimir';
          resultBody.textContent = data.message || 'Ocorreu um erro inesperado.';
        }
      } catch (err) {
        spinner.style.display = 'none';
        resultBox.style.display = 'block';
        resultBox.className = 'result error';
        resultTitle.textContent = '✖ Erro de conexão';
        resultBody.textContent = 'Não foi possível conectar ao servidor.';
      }

      compressBtn.disabled = false;
    });
  </script>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/compress', methods=['POST'])
def compress():
    file = request.files.get('pdf_file')
    quality = request.form.get('quality', 'ebook')

    if quality not in QUALITY_LEVELS:
        quality = 'ebook'

    if not file or not file.filename.endswith('.pdf'):
        return jsonify({'status': 'error', 'message': 'Por favor, envie um arquivo PDF válido.'}), 400

    name = file.filename.replace(" ", "_")
    in_p = os.path.join(UPLOAD_FOLDER, "in_" + name)
    out_p = os.path.join(UPLOAD_FOLDER, "out_" + name)

    try:
        file.save(in_p)
        original_size = os.path.getsize(in_p)

        if not compress_pdf(in_p, out_p, power=quality):
            return jsonify({'status': 'error', 'message': 'Falha ao executar o Ghostscript. Verifique se está instalado.'}), 500

        compressed_size = os.path.getsize(out_p)

        # Se comprimido ficou maior ou igual, não vale a pena
        if compressed_size >= original_size:
            os.remove(out_p)
            return jsonify({
                'status': 'already_small',
                'original_size': format_size(original_size),
                'compressed_size': format_size(compressed_size),
            })

        reduction_pct = (1 - compressed_size / original_size) * 100
        download_name = "vavilov_" + name

        # Mover para nome de download definitivo
        final_path = os.path.join(UPLOAD_FOLDER, download_name)
        os.rename(out_p, final_path)

        return jsonify({
            'status': 'ok',
            'original_size': format_size(original_size),
            'compressed_size': format_size(compressed_size),
            'reduction': f"{reduction_pct:.1f}%",
            'download_url': f'/download/{download_name}',
        })

    finally:
        # Limpa arquivo de entrada sempre
        if os.path.exists(in_p):
            os.remove(in_p)

@app.route('/download/<filename>')
def download(filename):
    # Segurança: apenas arquivos dentro de UPLOAD_FOLDER e prefixo vavilov_
    if not filename.startswith('vavilov_') or '/' in filename or '..' in filename:
        return jsonify({'status': 'error', 'message': 'Arquivo não encontrado.'}), 404
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({'status': 'error', 'message': 'Arquivo não encontrado ou expirado.'}), 404
    return send_file(path, as_attachment=True, download_name=filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095)
