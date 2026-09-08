"""Public multipart endpoint using the same editing engine as the platform."""
import asyncio
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import List, Literal
from urllib.parse import quote

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from engine.search import Finder


class Equivalence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["text", "regex", "dates"] = "text"
    query: str = Field("", max_length=2000)
    replace: str = Field("", max_length=2000)
    case_sensitive: bool = False
    whole_word: bool = False
    loose: bool = True

    @model_validator(mode="after")
    def valid_query(self):
        if self.kind != "dates" and not self.query:
            raise ValueError("query est obligatoire pour text et regex")
        error = Finder(id="validation", **self.model_dump()).error()
        if error:
            raise ValueError(error)
        return self


EXAMPLE = '[{"query":"Jean Dupont","replace":"Marc Dubois"},{"query":"2024","replace":"2025"}]'
DESCRIPTION = '''Remplace du texte dans un ou plusieurs PDF avec le moteur de la plateforme.

Envoyez un formulaire **multipart/form-data** : répétez `files` pour chaque PDF et
renseignez `equivalences` avec un tableau JSON de 1 à 24 règles. Exemple :
```json
[{"query":"Jean Dupont","replace":"Marc Dubois"},{"query":"2024","replace":"2025"}]
```
Chaque règle accepte `kind` (`text`, `regex`, `dates`), `query`, `replace`,
`case_sensitive`, `whole_word` et `loose`. Pour les dates, utilisez
`[{"kind":"dates"}]` et `date_rule={"type":"shift","years":1}` ou
`date_rule={"type":"map","pairs":{"2024-03-15":"2025-03-15"}}`.
Les règles s'appliquent au texte original (elles ne sont pas chaînées).

**Réponse** : un PDF pour un fichier, sinon un ZIP contenant chaque PDF et
`report.json` (résultats, avertissements, vérification). Les noms reçoivent le
suffixe `_1`. Sans correspondance, le fichier est retourné sans modification.
Les en-têtes `X-Replacements-Applied` et `X-Operation-Id` donnent le nombre de
remplacements et l'identifiant de l'opération.

**Limites** : 24 PDF, 40 Mio par fichier, 160 Mio au total, 800 pages par PDF,
4000 correspondances par document, 240 secondes de traitement.
Un fichier invalide, un remplacement refusé ou une vérification échouée produit
une erreur JSON pour le lot entier, sans téléchargement partiel.
Les PDF ne sont pas conservés après la requête. Le journal conserve les métadonnées,
les équivalences et les résultats, sans le contenu binaire des PDF.
Aucune clé API n'est nécessaire.
'''


def install_public_api(app, runtime):
    @app.post("/api/replace", tags=["API publique"], summary="Modifier un ou plusieurs PDF en un appel",
              description=DESCRIPTION, response_class=Response,
              responses={200: {"description": "PDF ou ZIP des résultats", "content": {
                  "application/pdf": {"schema": {"type": "string", "format": "binary"}},
                  "application/zip": {"schema": {"type": "string", "format": "binary"}}},
                  "headers": {name: {"schema": {"type": "string"}} for name in
                              ["Content-Disposition", "X-Operation-Id", "X-Replacements-Applied"]}},
                  400: {"description": "Fichier non PDF ou vide"},
                  413: {"description": "Limite de taille ou de nombre dépassée"},
                  422: {"description": "Équivalences invalides ou traitement refusé ; détail JSON"},
                  504: {"description": "Temps de traitement dépassé"}})
    async def replace_pdfs(request: Request, files: List[UploadFile] = File(..., description="Un ou plusieurs PDF (répéter le champ files)."),
                           equivalences: str = Form(..., description="Tableau JSON de règles query → replace. Voir l'exemple ci-dessus.", examples=[EXAMPLE]),
                           date_rule: str = Form('{}', description="Règle de dates au format JSON : shift, map, set, parts, text ou manual."),
                           locale: Literal["fr", "en"] = Form("fr"),
                           auto_repair: bool = Form(True), verify: bool = Form(True)):
        request.state.audit.update(event="replace", names=[runtime._safe_name(f.filename) for f in files])
        try:
            if not 1 <= len(files) <= runtime.MAX_DOCS:
                raise HTTPException(413, "Envoyez entre 1 et 24 PDF.")
            if len(equivalences) > 120000 or len(date_rule) > 120000:
                raise HTTPException(413, "Équivalences trop volumineuses.")
            try:
                rules = TypeAdapter(List[Equivalence]).validate_json(equivalences)
                if not 1 <= len(rules) <= 24:
                    raise ValueError("Envoyez entre 1 et 24 équivalences.")
                dates = runtime.DateRule.model_validate_json(date_rule).model_dump(exclude_none=True)
                if dates["type"] not in ("shift", "map", "set", "parts", "text", "manual"):
                    raise ValueError("Type de règle de dates invalide.")
                if any(r.kind == "dates" for r in rules) and date_rule.strip() == '{}':
                    raise ValueError("date_rule est obligatoire avec une équivalence dates.")
            except (ValidationError, ValueError) as exc:
                raise HTTPException(422, str(exc)) from exc
            config = {"equivalences": [{"id": f"f{i}", **r.model_dump()} for i, r in enumerate(rules)],
                      "date_rule": dates, "locale": locale, "verify": verify, "auto_repair": auto_repair, "files": []}
            request.state.audit.update(equivalences=config["equivalences"], date_rule=dates, verify=verify, auto_repair=auto_repair)
            # No batch, parser cache or output survives this request. Heavy work
            # has its own process, so even native parser allocations are freed.
            async with runtime._sem:
                with tempfile.TemporaryDirectory(prefix="atelier-api-") as directory:
                    folder = Path(directory)
                    total = 0
                    for index, upload in enumerate(files):
                        size = 0
                        with (folder / f"input-{index}.pdf").open("wb") as fh:
                            while chunk := await upload.read(1024 * 1024):
                                if size == 0 and not chunk.startswith(b"%PDF-"):
                                    raise HTTPException(400, f"{runtime._safe_name(upload.filename)} n'est pas un PDF.")
                                size += len(chunk)
                                total += len(chunk)
                                if size > runtime.MAX_FILE_BYTES or total > runtime.MAX_LOT_BYTES:
                                    raise HTTPException(413, "Maximum 40 Mio par PDF et 160 Mio au total.")
                                fh.write(chunk)
                        if not size:
                            raise HTTPException(400, "Un PDF envoyé est vide.")
                        config["files"].append({"filename": runtime._safe_name(upload.filename), "size": size})
                    request.state.audit.update(files=config["files"], bytes=total, docs=len(files))
                    (folder / "request.json").write_text(json.dumps(config))
                    proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("public_worker.py")), directory,
                                                                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=240)
                    except asyncio.TimeoutError:
                        raise HTTPException(504, "Le traitement a dépassé 240 secondes.")
                    finally:
                        if proc.returncode is None:
                            proc.kill()
                            await proc.wait()
                    if proc.returncode != 0:
                        raise HTTPException(422, "Le moteur n'a pas pu traiter ces PDF.")
                    report = json.loads((folder / "report.json").read_text())
                    request.state.audit["documents"] = report["documents"]
                    if any(d.get("error") for d in report["documents"]):
                        raise HTTPException(422, report)
                    applied = sum(d["applied"] for d in report["documents"])
                    request.state.audit["applied"] = applied
                    headers = {"X-Replacements-Applied": str(applied), "Cache-Control": "no-store"}
                    if len(files) == 1:
                        filename = runtime._output_name(config["files"][0]["filename"])
                        headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(filename, safe="")
                        return Response((folder / "output-0.pdf").read_bytes(), media_type="application/pdf", headers=headers)
                    buf = io.BytesIO()
                    used = {"report.json"}
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
                        for index, item in enumerate(config["files"]):
                            original = runtime._output_name(item["filename"])
                            name, suffix = original, 2
                            while name in used:
                                name = f"{Path(original).stem} ({suffix}){Path(original).suffix}"
                                suffix += 1
                            used.add(name)
                            archive.write(folder / f"output-{index}.pdf", name)
                            report["documents"][index]["output_filename"] = name
                        archive.writestr("report.json", json.dumps(report, ensure_ascii=False, indent=2))
                    headers["Content-Disposition"] = 'attachment; filename="atelier-pdf.zip"'
                    return Response(buf.getvalue(), media_type="application/zip", headers=headers)
        finally:
            for upload in files:
                await upload.close()
