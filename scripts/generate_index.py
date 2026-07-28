#!/usr/bin/env python3
"""
generate_index.py — genera una página de inicio simple con links a todos los
dashboards ya enriquecidos que haya en la carpeta dashboards/.

Uso:
    python3 generate_index.py dashboards/ index.html
"""
import sys
import os
import glob
import html as htmlmod
import datetime

def main():
    if len(sys.argv) < 3:
        print("Uso: python3 generate_index.py dashboards/ index.html")
        sys.exit(1)

    dash_dir = sys.argv[1]
    out_path = sys.argv[2]

    files = sorted(glob.glob(os.path.join(dash_dir, "*.html")))

    rows = []
    for f in files:
        name = os.path.basename(f)
        title = name.rsplit(".html", 1)[0].replace("_", " ").replace("-", " ")
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M")
        rows.append(
            f'<li><a href="{htmlmod.escape(dash_dir.rstrip("/"))}/{htmlmod.escape(name)}">'
            f'{htmlmod.escape(title)}</a> '
            f'<span class="meta">actualizado {mtime}</span></li>'
        )

    body = "\n      ".join(rows) if rows else "<li>Todavía no hay dashboards publicados.</li>"

    page = f"""<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboards QGIS</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 720px;
         margin: 48px auto; padding: 0 16px; color: #1f2430; }}
  h1 {{ font-size: 22px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: 12px 0; border-bottom: 1px solid #e5e7eb; }}
  a {{ font-size: 16px; color: #1a56db; text-decoration: none; font-weight: 600; }}
  a:hover {{ text-decoration: underline; }}
  .meta {{ display: block; font-size: 12px; color: #6b7280; margin-top: 2px; }}
</style>
</head>
<body>
  <h1>Dashboards publicados</h1>
  <ul>
      {body}
  </ul>
</body>
</html>
"""
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(page)
    print(f"Índice generado con {len(files)} dashboard(s) -> {out_path}")


if __name__ == "__main__":
    main()
