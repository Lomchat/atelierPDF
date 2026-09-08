"""One request per process: PDF parsers and buffers die with this process."""
import json
import sys
from pathlib import Path

import fitz
from engine.document import analyze, drop_cached, process
from engine.fontrepair import FontLibrary
from engine.search import Finder


def run(folder):
    config = json.loads((folder / "request.json").read_text())
    library = FontLibrary(extra_dirs=[str(Path(__file__).resolve().parents[1] / "data" / "fonts")])
    library.scan()
    finders = [Finder(**f) for f in config["equivalences"]]
    reports = []
    for index, item in enumerate(config["files"]):
        report = dict(item)
        reports.append(report)
        try:
            raw = (folder / f"input-{index}.pdf").read_bytes()
            with fitz.open(stream=raw, filetype="pdf") as pdf:
                if pdf.needs_pass:
                    raise ValueError("PDF protégé par un mot de passe.")
                report["pages"] = len(pdf)
                if len(pdf) > 800:
                    raise ValueError("Maximum 800 pages par PDF.")
            key = f"public-{index}"
            try:
                analysis = analyze(raw, finders, config["date_rule"], config["locale"], cache_key=key)
                report["warnings"] = analysis.warnings
                if analysis.truncated:
                    raise ValueError("Plus de 4000 correspondances : divisez le document ou précisez les équivalences.")
            finally:
                drop_cached(key)
            outcome = process(raw, finders=finders, date_rule=config["date_rule"],
                              locale_hint=config["locale"], auto_repair=config["auto_repair"],
                              verify=config["verify"], font_library=library)
            report.update(outcome.to_dict())
            if outcome.refused or outcome.notdef or (outcome.verification or {}).get("acceptable") is False:
                report["error"] = "Des remplacements ont été refusés ou la vérification visuelle a échoué. Utilisez la plateforme pour les inspecter."
            else:
                (folder / f"output-{index}.pdf").write_bytes((outcome.pdf_bytes or raw) if outcome.applied else raw)
        except Exception as exc:
            report["error"] = str(exc)
    (folder / "report.json").write_text(json.dumps({"documents": reports}, ensure_ascii=False))


if __name__ == "__main__":
    run(Path(sys.argv[1]))
