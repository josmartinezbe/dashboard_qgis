# Dashboard — prueba en GitHub Pages

Este repo contiene el export del plugin **QGIS Dashboards** ya enriquecido
(`scripts/enrich_dashboard.py`) con:

- Fix del bug de `connections` (array/objeto mixto que dejaba la página en blanco).
- Colores por categoría en el mapa + leyenda funcional (paleta genérica, sin
  usar aún los estilos exactos de QGIS — eso queda pendiente para una segunda
  iteración).
- Filtro por pan/zoom: Chart, List, Indicator y Pivot se recalculan según lo
  que esté visible en el mapa, sin reconstruirlo.

`index.html` es justamente ese archivo ya procesado — lo abre directo
GitHub Pages, no requiere build ni backend.

## Publicar (primera vez)

Desde esta carpeta:

```bash
git init
git add .
git commit -m "Primer export enriquecido del dashboard"
git branch -M main
git remote add origin https://github.com/TU-USUARIO/TU-REPO.git
git push -u origin main
```

Luego en GitHub: **Settings → Pages → Source: Deploy from a branch → Branch: main / (root)**.

En unos minutos queda publicado en:
`https://TU-USUARIO.github.io/TU-REPO/`

## Actualizar con un nuevo export (automático — recomendado)

Ya no necesitas correr nada en tu computador ni pedirle ayuda a nadie para
esto. El repo tiene un GitHub Action (`.github/workflows/enrich.yml`) que
hace todo el trabajo:

1. Exporta el dashboard nuevo desde el plugin en QGIS (el HTML "crudo", sin tocar).
2. Súbelo a la carpeta `raw/` del repo, con cualquier nombre
   (ej. `raw/export_2026-08-01.html`). Puedes hacerlo:
   - Arrastrando el archivo directamente en la web de GitHub (Add file → Upload files → carpeta `raw/`), o
   - Por línea de comandos: `git add raw/export_nuevo.html && git commit -m "nuevo export" && git push`
3. Eso dispara automáticamente el Action, que:
   - Toma el export más reciente de `raw/`
   - Le aplica los 3 parches (`scripts/enrich_dashboard.py`)
   - Sobreescribe `index.html` con el resultado
   - Hace commit y push solo
4. GitHub Pages se actualiza solo, 1-2 minutos después.

Puedes ver el progreso en la pestaña **Actions** del repo — cada corrida te
dice exactamente qué parches se aplicaron (igual que cuando lo corres local).

**No necesitas borrar los exports viejos de `raw/`** — el Action siempre usa
el más reciente, pero puedes limpiarlos cuando quieras.

## Actualizar manualmente (alternativa, si prefieres no usar Actions)

```bash
python3 scripts/enrich_dashboard.py nuevo_export.html index.html
git add index.html
git commit -m "Actualiza dashboard"
git push
```

## Pendiente / próximos pasos

- [ ] Usar los colores exactos del proyecto QGIS (vía estilos de qgis2web) en
      vez de la paleta genérica.
- [ ] Simplificar geometría en PostGIS (`ST_SimplifyPreserveTopology`) antes
      de exportar, para bajar el peso del archivo.
- [ ] Automatizar el enriquecimiento con GitHub Actions (opcional).
