# vavilov-small-pdf
# vavilov-small-pdf

## Ghostscript

O Ghostscript é necessário para comprimir PDFs. A imagem Docker instala essa
dependência durante o build. Para recriá-la e iniciar o serviço:

```sh
docker compose up --build -d
```

Se executar a aplicação diretamente no macOS, instale o Ghostscript antes de
iniciá-la:

```sh
brew install ghostscript
```

## Divisão de PDFs

A interface permite comprimir um PDF e, opcionalmente, dividi-lo quando o
resultado ultrapassar o tamanho máximo configurado. Também é possível dividir
manualmente por tamanho, quantidade de páginas ou intervalos. Os presets de
portais são referências provisórias: confirme os limites vigentes na
documentação oficial do destino.

O limite de envio da aplicação é 1 GiB (`MAX_CONTENT_LENGTH` em `app.py`).
Se houver um proxy reverso à frente do Flask, configure nele um limite de
requisição compatível. Arquivos e resultados antigos são removidos na próxima
requisição após uma hora.

Instale as dependências Python com `pip install -r requirements.txt` ao
executar fora do Docker. Além do Flask e Werkzeug, a divisão de páginas usa
`pypdf`; o Ghostscript continua necessário para comprimir.
