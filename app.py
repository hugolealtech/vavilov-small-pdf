import os
import subprocess
from flask import Flask, request, render_template, send_file

app = Flask(__name__)
UPLOAD_FOLDER = 'uploads'
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def compress_pdf(input_path, output_path, power='ebook'):
    # Comando do Ghostscript para esmagar o arquivo
    gs_command = [
        "gs", "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS=/{power}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={output_path}", input_path
    ]
    try:
        subprocess.run(gs_command, check=True)
        return True
    except Exception as e:
        print(f"Erro: {e}")
        return False

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        file = request.files.get('pdf_file')
        if file and file.filename.endswith('.pdf'):
            name = file.filename.replace(" ", "_")
            in_p = os.path.join(UPLOAD_FOLDER, "in_" + name)
            out_p = os.path.join(UPLOAD_FOLDER, "out_" + name)
            file.save(in_p)
            if compress_pdf(in_p, out_p):
                return send_file(out_p, as_attachment=True, download_name="vavilov_" + name)
    return render_template('index.html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8095)
