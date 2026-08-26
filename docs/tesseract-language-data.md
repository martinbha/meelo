# Tesseract language data

Production startup and `/health/` require the English (`eng`) and Korean (`kor`)
Tesseract language packs. Verification uses only the local Tesseract binary and
the `.traineddata` files under `TESSDATA_PREFIX`; it does not contact a network
service.

The production image installs `tesseract-ocr-eng` and `tesseract-ocr-kor` from
its Debian package repository and sets:

```text
TESSDATA_PREFIX=/usr/share/tesseract-ocr/5/tessdata
```

Each Tesseract OCR run records the binary version and a SHA-256 content version
for every language-data file it used. A missing installed language or unreadable
`.traineddata` file prevents production startup and makes the health endpoint
return HTTP 503 with the missing pack name.

## Add a language

1. Add the distribution package for the language to the `apt-get install` line
   in `Dockerfile`. Debian packages use the form `tesseract-ocr-<pack>`, such as
   `tesseract-ocr-deu` for German.
2. Add the application's language code and Tesseract pack name to
   `LANGUAGE_PACKS` in `apps/ocr/tesseract.py`, for example `"de": "deu"`.
3. Rebuild the image. Startup verification automatically includes every pack in
   `LANGUAGE_PACKS` and fails if the package or traineddata file is absent.
4. Run the Tesseract adapter tests and build the production image. Start the
   image without network access and confirm `/health/` lists the new pack.

Do not download traineddata during application startup. Provision it while the
image is built so the deployed OCR path remains local and reproducible.
