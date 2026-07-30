#!/usr/bin/env python3
"""
enrich_dashboard.py — aplica automáticamente a cualquier export del plugin
QGIS Dashboards los 4 arreglos/mejoras que fuimos construyendo a mano:

  1. FIX  connections mixto array/objeto -> TypeError que deja la página en blanco.
  2. MEJORA colores por categoría en el mapa + leyenda funcional (usa el color
     real por feature si existe un campo tipo codigo_color; si no, genera
     una paleta automática).
  3. MEJORA filtro por extensión: Chart/List/Indicator/Pivot se recalculan al
     hacer pan/zoom en el mapa, sin reconstruir el mapa (no pierdes tu posición).
  4. MEJORA el propio mapa oculta/muestra polígonos según la selección activa
     (Category selector, clic en una barra del Chart, etc.) -- antes el mapa
     siempre dibujaba todos los polígonos sin importar el filtro.

Uso:
    python3 enrich_dashboard.py entrada.html [salida.html]

Es idempotente: si corres el script dos veces sobre el mismo archivo ya
enriquecido, detecta la marca y no vuelve a aplicar los parches.
"""
import sys
import json
import re

MARKER = "/* DASH_ENRICHED_V1 */"

FILL_OPACITY = 0.55  # antes 0.75 -- más transparencia para ver mejor el satelital de fondo

LEGEND_TEXT_COLOR = "#222"  # negro -- cambia aquí si quieres otro color

# Renombra aquí lo que quieras mostrar en la leyenda, sin tocar el resto del
# script. Clave = valor real de la categoría (tal cual está en los datos),
# Valor = lo que quieres que se vea en la leyenda. Lo que no esté aquí se
# muestra tal cual viene de los datos.
LEGEND_LABEL_OVERRIDES = {
    
    "1. ÓPTIMA - GRAN ESCALA Y DEMANDA": "1. Muy alta",
    "2. ALTA - OPORTUNIDAD": "2. Alta",
    "3. MEDIA - BUENA DENSIDAD": "3. Media",
    "4. BAJA - POTENCIAL MODESTO": "4. Baja",
    "5. MUY BAJA - PROYECTO AISLADO": "5. Muy baja",
}

ESRI_SATELLITE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_SATELLITE_ATTRIBUTION = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"

QUALITATIVE_PALETTE = [
    "rgb(215,25,28)", "rgb(43,131,186)", "rgb(230,200,60)",
    "rgb(26,150,65)", "rgb(148,103,189)", "rgb(255,127,0)",
    "rgb(23,190,207)", "rgb(227,119,194)",
]


def load_dashboard_json(html):
    start = html.find('<script type="application/json" id="dashboard-data">')
    if start == -1:
        return None, None, None
    start2 = html.find("\n", start) + 1
    end = html.find("</script>", start2)
    data_text = html[start2:end]
    return json.loads(data_text), start2, end


def detect_category_field(d):
    """Look through tiles for a chart/category_selector that names a category_field
    and layer_id, so we know which field/layer to color by."""
    for page in d.get("pages", []):
        for t in page.get("tiles", []):
            cfg = t.get("config", {})
            field = cfg.get("category_field")
            layer_id = t.get("layer_id") or cfg.get("layer_id")
            if field and layer_id and layer_id in d.get("layers", {}):
                return layer_id, field
    return None, None


COLOR_FIELD_CANDIDATES = ["codigo_color", "color", "hex_color", "map_color"]


def detect_explicit_color_field(d, layer_id):
    """If the layer already carries a per-feature hex color computed in SQL
    (e.g. codigo_color), prefer that over generating a palette — it's exact,
    not guessed, and stays correct even if categories/order change later."""
    layer = d["layers"].get(layer_id, {})
    fields = layer.get("fields", [])
    for cand in COLOR_FIELD_CANDIDATES:
        if cand in fields:
            return cand
    return None


def natural_key(s):
    """Ordena '1. ÓPTIMA...', '2. ALTA...' ... numéricamente por el prefijo,
    en vez de alfabéticamente (que pondría '1.' antes de '10.' pero después
    de cualquier letra, y mezclaría el orden como vimos en la leyenda)."""
    s = str(s)
    m = re.match(r'^\s*(\d+)', s)
    if m:
        return (0, int(m.group(1)), s)
    return (1, 0, s)


def build_color_map_from_field(d, layer_id, category_field, color_field):
    """Build {category_value: hex_color} by reading the real, already-computed
    color per feature straight from the data — no palette guessing. Se ordena
    con natural_key así el diccionario (y por lo tanto la leyenda, que itera
    Object.keys() en orden de inserción) sale en orden lógico, no en el orden
    en que las categorías aparecieron primero en los datos."""
    layer = d["layers"][layer_id]
    raw = {}
    for feat in layer.get("features", []):
        cat = feat.get(category_field)
        col = feat.get(color_field)
        if cat is not None and col and cat not in raw:
            raw[cat] = col
    return {k: raw[k] for k in sorted(raw.keys(), key=natural_key)}


def build_color_map(d, layer_id, field):
    layer = d["layers"][layer_id]
    values = []
    seen = set()
    for feat in layer.get("features", []):
        v = feat.get(field)
        if v is not None and v not in seen:
            seen.add(v)
            values.append(v)
    values.sort(key=natural_key)
    return {v: QUALITATIVE_PALETTE[i % len(QUALITATIVE_PALETTE)] for i, v in enumerate(values)}


def js_str(s):
    return json.dumps(s, ensure_ascii=False)


def patch_connections_guard(html):
    old = "if (conns[src].indexOf(tile.id) >= 0) {"
    new = "if (Array.isArray(conns[src]) && conns[src].indexOf(tile.id) >= 0) {"
    if old in html:
        return html.replace(old, new, 1), True
    return html, False


def patch_category_colors_and_legend(html, field, color_map):
    applied = []

    color_js = "{\n" + ",\n".join(
        f"        {js_str(k)}: {js_str(v)}" for k, v in color_map.items()
    ) + "\n      }"

    # 0) make the category colors GLOBAL (not just local to the map's style
    # function), and add a catColor() helper, so the Chart widget (bars, pie,
    # lollipop, dot, funnel, treemap...) can use the exact same colors as the
    # map instead of the plugin's own generic/unordered palette.
    old_anchor = 'function color(i) { return SERIES[i % SERIES.length]; }'
    new_anchor = (
        old_anchor + '\n'
        f'  var DASH_CATEGORY_FIELD = {js_str(field)};\n'
        f'  var DASH_CATEGORY_COLORS = {color_js};\n'
        '  function catColor(cat, i) {\n'
        '    var c = DASH_CATEGORY_COLORS ? DASH_CATEGORY_COLORS[cat] : null;\n'
        '    return c || color(i);\n'
        '  }'
    )
    if old_anchor in html and html.count(old_anchor) == 1:
        html = html.replace(old_anchor, new_anchor, 1)
        applied.append("global category colors + catColor()")

        # every chart painter that colors bars/points/segments by category and
        # already distinguishes the selected one uses this exact pattern
        old_sel = '(d[0] === selKey) ? "var(--muted)" : color(i);'
        new_sel = '(d[0] === selKey) ? "var(--muted)" : catColor(d[0], i);'
        n_sel = html.count(old_sel)
        if n_sel:
            html = html.replace(old_sel, new_sel)
            applied.append(f"chart bars/points recolored ({n_sel} painters)")

        # pie/donut: segment fill + legend swatch
        old_pie1 = 'fill: color(i), stroke: "#ffffff", "stroke-width": 1, cursor: "pointer" });'
        new_pie1 = 'fill: catColor(d[0], i), stroke: "#ffffff", "stroke-width": 1, cursor: "pointer" });'
        if old_pie1 in html and html.count(old_pie1) == 1:
            html = html.replace(old_pie1, new_pie1, 1)
            applied.append("pie/donut segments recolored")

        old_pie2 = 'fill: color(i) }));'
        new_pie2 = 'fill: catColor(d[0], i) }));'
        if old_pie2 in html and html.count(old_pie2) == 1:
            html = html.replace(old_pie2, new_pie2, 1)
            applied.append("pie/donut legend swatches recolored")

    old_style = (
        '      var col = color(idx);\n'
        '      var gj = L.geoJSON(fc, {\n'
        '        style: function () {\n'
        '          return { color: col, weight: 2, fillColor: col, fillOpacity: 0.25 };\n'
        '        },'
    )
    new_style = (
        '      var col = color(idx);\n'
        '      var gj = L.geoJSON(fc, {\n'
        '        style: function (f) {\n'
        '          var val = f && f.properties ? f.properties[DASH_CATEGORY_FIELD] : null;\n'
        '          var c = (DASH_CATEGORY_COLORS && DASH_CATEGORY_COLORS[val]) || col;\n'
        '          return { color: "#f7f7f7", weight: 1, fillColor: c, fillOpacity: ' + str(FILL_OPACITY) + ' };\n'
        '        },'
    )
    if old_style in html:
        html = html.replace(old_style, new_style, 1)
        applied.append("map category colors")

    old_dispatch = (
        '    } else {\n'
        '      body.appendChild(el("div", "dash-note", tile.type));\n'
        '    }\n'
        '    return node;\n'
        '  }'
    )
    legend_fn = (
        '  function renderLegend(body, tile) {\n'
        '    var wrap = el("div", "dash-legend");\n'
        '    wrap.style.padding = "8px";\n'
        '    wrap.style.fontSize = "12px";\n'
        '    var title = el("div", "dash-legend-title", (tile.config && tile.config.title) || '
        + js_str(field) + ');\n'
        '    title.style.fontWeight = "700";\n'
        '    title.style.marginBottom = "6px";\n'
        f'    title.style.color = {js_str(LEGEND_TEXT_COLOR)};\n'
        '    wrap.appendChild(title);\n'
        '    Object.keys(DASH_CATEGORY_COLORS || {}).forEach(function (label) {\n'
        '      var row = el("div", "dash-legend-row");\n'
        '      row.style.display = "flex";\n'
        '      row.style.alignItems = "center";\n'
        '      row.style.gap = "8px";\n'
        '      row.style.marginBottom = "4px";\n'
        '      var swatch = el("span", "dash-legend-swatch");\n'
        '      swatch.style.display = "inline-block";\n'
        '      swatch.style.width = "14px";\n'
        '      swatch.style.height = "14px";\n'
        '      swatch.style.background = DASH_CATEGORY_COLORS[label];\n'
        '      swatch.style.border = "1px solid #ccc";\n'
        f'      var displayLabel = ({json.dumps(LEGEND_LABEL_OVERRIDES, ensure_ascii=False)})[label] || label;\n'
        '      var text = el("span", "dash-legend-label", displayLabel);\n'
        f'      text.style.color = {js_str(LEGEND_TEXT_COLOR)};\n'
        '      row.appendChild(swatch);\n'
        '      row.appendChild(text);\n'
        '      wrap.appendChild(row);\n'
        '    });\n'
        '    body.appendChild(wrap);\n'
        '  }'
    )
    new_dispatch = (
        '    } else if (tile.type === "legend") {\n'
        '      renderLegend(body, tile);\n'
        '    } else {\n'
        '      body.appendChild(el("div", "dash-note", tile.type));\n'
        '    }\n'
        '    return node;\n'
        '  }\n\n' + legend_fn
    )
    if old_dispatch in html:
        html = html.replace(old_dispatch, new_dispatch, 1)
        applied.append("legend widget")

    return html, applied


def patch_indicator_style_color(html):
    """El plugin SÍ guarda el color que configuras en QGIS para el Indicator
    (cfg.style.value_color), pero el runtime del export lo ignora y usa el
    color de acento del tema (var(--accent)) para todos los indicadores por
    igual. Este parche hace que respete el color que de verdad configuraste."""
    old = (
        '    var valNode = el("div", "dash-ind-value");\n'
        '    if (cfg.value_size) valNode.style.fontSize = cfg.value_size + "px";\n'
        '    wrap.appendChild(valNode);'
    )
    new = (
        '    var valNode = el("div", "dash-ind-value");\n'
        '    if (cfg.value_size) valNode.style.fontSize = cfg.value_size + "px";\n'
        '    if (cfg.style && cfg.style.value_color) valNode.style.color = cfg.style.value_color;\n'
        '    wrap.appendChild(valNode);'
    )
    if old not in html or html.count(old) != 1:
        return html, False
    html = html.replace(old, new, 1)
    return html, True


def patch_polygon_labels(html, label_field, min_zoom=13):
    """Agrega etiquetas de texto sobre cada polígono del mapa, usando el campo
    indicado -- pero de forma PEREZOSA: solo crea los tooltips en el DOM
    cuando el zoom ya es suficiente para verlos, y los destruye al alejar el
    zoom. Crear miles de tooltips permanentes de una sola vez al cargar (lo
    que hacía la versión anterior) congela el navegador con capas grandes."""

    old_decl = 'var m = tile.map || {};'
    if html.count(old_decl) != 1:
        return html, False
    html = html.replace(old_decl, old_decl + '\n    var LABEL_LAYERS = [];', 1)

    old = (
        '        onEachFeature: function (f, lyr) {\n'
        '          lyr.bindPopup(identifyHtml(layer.fields, f.properties));\n'
        '        }\n'
        '      }).addTo(map);'
    )
    new = (
        '        onEachFeature: function (f, lyr) {\n'
        '          lyr.bindPopup(identifyHtml(layer.fields, f.properties));\n'
        f'          var labelVal = f.properties ? f.properties[{js_str(label_field)}] : null;\n'
        '          if (labelVal !== null && labelVal !== undefined && labelVal !== "") {\n'
        '            lyr._dashLabel = String(labelVal);\n'  # cheap: no DOM work yet
        '          }\n'
        '        }\n'
        '      }).addTo(map);\n'
        '      LABEL_LAYERS.push(gj);'
    )
    if old not in html:
        return html, False
    html = html.replace(old, new, 1)

    old2 = '    var ext = m.extent;'
    if html.count(old2) != 1:
        return html, False
    new2 = (
        '    if (!document.getElementById("dash-poly-label-style")) {\n'
        '      var lst = document.createElement("style");\n'
        '      lst.id = "dash-poly-label-style";\n'
        '      lst.textContent = ".dash-poly-label{background:transparent;border:none;'
        'box-shadow:none;font-size:11px;font-weight:600;color:#222;'
        'text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;}";\n'
        '      document.head.appendChild(lst);\n'
        '    }\n'
        f'    var LABEL_MIN_ZOOM = {min_zoom};\n'
        '    var updateLabelVisibility = function () {\n'
        '      var show = map.getZoom() >= LABEL_MIN_ZOOM;\n'
        '      var vb = show ? map.getBounds() : null;\n'
        '      LABEL_LAYERS.forEach(function (glGroup) {\n'
        '        glGroup.eachLayer(function (lyr) {\n'
        '          if (!lyr._dashLabel) return;\n'
        '          if (show) {\n'
        '            var lb = lyr.getBounds ? lyr.getBounds() : null;\n'
        '            var inView = !lb || vb.intersects(lb);\n'
        '            if (inView && !lyr.getTooltip()) {\n'
        '              lyr.bindTooltip(lyr._dashLabel, {\n'
        '                permanent: true, direction: "center", className: "dash-poly-label"\n'
        '              });\n'
        '            } else if (!inView && lyr.getTooltip()) {\n'
        '              lyr.unbindTooltip();\n'
        '            }\n'
        '          } else if (lyr.getTooltip()) {\n'
        '            lyr.unbindTooltip();\n'
        '          }\n'
        '        });\n'
        '      });\n'
        '    };\n'
        '    map.on("zoomend moveend", updateLabelVisibility);\n'
        '    updateLabelVisibility();\n'
        '    var ext = m.extent;'
    )
    html = html.replace(old2, new2, 1)
    return html, True


def patch_satellite_basemap(html, start2, end, d, url_template, attribution, max_zoom=19):
    """Cambia el basemap de todos los tiles de mapa a imágenes satelitales
    (o cualquier tile server que le pases), editando directamente el JSON
    embebido -- no hace falta tocar el runtime JS para esto."""
    changed = False
    for page in d.get("pages", []):
        for t in page.get("tiles", []):
            if t.get("type") == "map":
                m = t.setdefault("map", {})
                bm = m.setdefault("basemap", {})
                bm["url_template"] = url_template
                bm["attribution"] = attribution
                bm["max_zoom"] = max_zoom
                changed = True
    if not changed:
        return html, d, False
    new_json = json.dumps(d, ensure_ascii=False)
    html = html[:start2] + new_json + html[end:]
    return html, d, True


def patch_selector_numeric_sort(html):
    """El <select> del Category selector ordena las opciones con .sort() por
    defecto, que es orden de TEXTO (1, 12, 13, 2, 3...). Lo cambia a un orden
    natural: numérico cuando el valor es un número, alfabético si no."""
    old = 'Object.keys(seen).sort().forEach(function (v) { select.appendChild(new Option(v, v)); });'
    new = (
        'Object.keys(seen).sort(function (a, b) {\n'
        '        var na = Number(a), nb = Number(b);\n'
        '        var an = a !== "" && !isNaN(na), bn = b !== "" && !isNaN(nb);\n'
        '        if (an && bn) return na - nb;\n'
        '        return a < b ? -1 : (a > b ? 1 : 0);\n'
        '      }).forEach(function (v) { select.appendChild(new Option(v, v)); });'
    )
    if old not in html or html.count(old) != 1:
        return html, False
    html = html.replace(old, new, 1)
    return html, True


def patch_map_visual_filter(html):
    """Hoy el mapa dibuja SIEMPRE todos los polígonos, sin importar qué haya
    seleccionado un Category selector o un clic en el Chart -- solo esos otros
    widgets se recalculan. Este parche hace que el propio mapa oculte/muestre
    polígonos según las selecciones activas que lo tengan como destino."""
    old_anchor = 'var MAP_INSTANCES = [];  // live L.map objects, torn down on page switch'
    new_anchor = old_anchor + '''

  function mapPredsFor(tileId, page) {
    if (!page) return [];
    var conns = page.connections || {};
    var sel = selections(page.id);
    var preds = [];
    Object.keys(conns).forEach(function (src) {
      if (Array.isArray(conns[src]) && conns[src].indexOf(tileId) >= 0) {
        var s = sel[src];
        if (s && s.pairs && s.pairs.length) preds = preds.concat(s.pairs);
      }
    });
    return preds;
  }

  function applyMapPreds(layer, preds) {
    if (!preds.length) return layer;
    var feats = [], geoms = [];
    (layer.features || []).forEach(function (r, i) {
      var ok = preds.every(function (p) {
        if (p.idxSet) return true;
        if (p.lo !== undefined) {
          var n = parseFloat(r[p.field]);
          return !isNaN(n) && n >= p.lo && n < p.hi;
        }
        return String(r[p.field]) === String(p.value);
      });
      if (ok) { feats.push(r); geoms.push((layer.geometry || [])[i]); }
    });
    return { features: feats, geometry: geoms, fields: layer.fields };
  }'''
    if html.count(old_anchor) != 1:
        return html, False
    html = html.replace(old_anchor, new_anchor, 1)

    old_fc = '''      var layer = DATA.layers[lid];
      if (!layer || !layer.geometry) return;
      var fc = featureCollection(layer);'''
    new_fc = '''      var layer = DATA.layers[lid];
      if (!layer || !layer.geometry) return;
      var preds = mapPredsFor(tile.id, page);
      var fc = featureCollection(applyMapPreds(layer, preds));'''
    if html.count(old_fc) != 1:
        return html, False
    html = html.replace(old_fc, new_fc, 1)
    return html, True


def patch_map_controls(html):
    """Agrega un panel flotante sobre el mapa con: (1) selector de mapa base
    (Satelital/Calles/Claro/Topográfico) y (2) control deslizante de
    transparencia de los polígonos."""
    old_tile = '''    L.tileLayer(bm.url_template ||
      "https://tile.openstreetmap.org/{z}/{x}/{y}.png", opts).addTo(map);'''
    new_tile = '''    var baseLayer = L.tileLayer(bm.url_template ||
      "https://tile.openstreetmap.org/{z}/{x}/{y}.png", opts).addTo(map);'''
    if html.count(old_tile) != 1:
        return html, False
    html = html.replace(old_tile, new_tile, 1)

    old_anchor = '    var ext = m.extent;'
    if html.count(old_anchor) != 1:
        return html, False
    new_anchor = '''    (function () {
      var BASEMAP_OPTIONS = [
        { label: "Satelital (Esri)", url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", attribution: "Tiles &copy; Esri" },
        { label: "Calles (OSM)", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: "&copy; OpenStreetMap contributors" },
        { label: "Claro (Carto)", url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", attribution: "&copy; OpenStreetMap contributors &copy; CARTO" },
        { label: "Topogr\\u00e1fico (Esri)", url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", attribution: "Tiles &copy; Esri" }
      ];
      if (!document.getElementById("dash-map-controls-style")) {
        var st = document.createElement("style");
        st.id = "dash-map-controls-style";
        st.textContent = ".dash-map-controls{position:absolute;top:8px;right:8px;z-index:1000;" +
          "background:rgba(255,255,255,.92);border:1px solid #d7dbe0;border-radius:8px;padding:8px 10px;" +
          "font-size:11px;font-family:inherit;box-shadow:0 1px 4px rgba(0,0,0,.15);display:flex;flex-direction:column;gap:6px;min-width:150px;}" +
          ".dash-map-controls label{font-weight:600;color:#333;margin-bottom:2px;display:block;}" +
          ".dash-map-controls select,.dash-map-controls input[type=range]{width:100%;box-sizing:border-box;}";
        document.head.appendChild(st);
      }
      var panel = document.createElement("div");
      panel.className = "dash-map-controls";
      panel.innerHTML =
        '<div><label>Mapa base</label><select class="dash-basemap-select"></select></div>' +
        '<div><label>Transparencia</label><input type="range" class="dash-opacity-range" min="0" max="100" value="''' + str(int(FILL_OPACITY * 100)) + '''"></div>';
      host.appendChild(panel);

      var sel = panel.querySelector(".dash-basemap-select");
      BASEMAP_OPTIONS.forEach(function (opt, oi) {
        var o = document.createElement("option");
        o.value = String(oi);
        o.textContent = opt.label;
        if (opt.url === (bm.url_template || "")) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () {
        var opt = BASEMAP_OPTIONS[Number(sel.value)];
        if (!opt) return;
        map.removeLayer(baseLayer);
        baseLayer = L.tileLayer(opt.url, { maxZoom: bm.max_zoom || 19, attribution: opt.attribution }).addTo(map);
        baseLayer.bringToBack();
      });

      var range = panel.querySelector(".dash-opacity-range");
      range.addEventListener("input", function () {
        var v = Number(range.value) / 100;
        gjLayers.forEach(function (gl) { gl.gj.setStyle({ fillOpacity: v }); });
      });
    })();

    var ext = m.extent;'''
    html = html.replace(old_anchor, new_anchor, 1)
    return html, True


def patch_extent_filter(html):
    applied = []

    def do(old, new, label):
        nonlocal html
        if old in html:
            html = html.replace(old, new, 1)
            applied.append(label)
            return True
        return False

    do(
'''  function featureCollection(layer) {
    var rows = layer.features || [];
    var geoms = layer.geometry || [];
    var feats = [];
    for (var i = 0; i < rows.length; i++) {
      if (!geoms[i]) continue;
      feats.push({ type: "Feature", geometry: geoms[i], properties: rows[i] });
    }
    return { type: "FeatureCollection", features: feats };
  }''',
'''  function featureCollection(layer) {
    var rows = layer.features || [];
    var geoms = layer.geometry || [];
    var feats = [];
    for (var i = 0; i < rows.length; i++) {
      if (!geoms[i]) continue;
      if (rows[i].__idx === undefined) rows[i].__idx = i;
      feats.push({ type: "Feature", geometry: geoms[i], properties: rows[i] });
    }
    return { type: "FeatureCollection", features: feats };
  }''',
        "row index tagging")

    do(
'''    return rows.filter(function (r) {
      return preds.every(function (p) {
        if (p.lo !== undefined) {          // numeric range predicate (histogram)
          var n = parseFloat(r[p.field]);
          return !isNaN(n) && n >= p.lo && n < p.hi;
        }
        return eq(r[p.field], p.value);
      });
    });
  }''',
'''    return rows.filter(function (r) {
      return preds.every(function (p) {
        if (p.idxSet) {
          if (p.layerId && tile.layer_id && p.layerId !== tile.layer_id) return true;
          return p.idxSet.has(r.__idx);
        }
        if (p.lo !== undefined) {          // numeric range predicate (histogram)
          var n = parseFloat(r[p.field]);
          return !isNaN(n) && n >= p.lo && n < p.hi;
        }
        return eq(r[p.field], p.value);
      });
    });
  }''',
        "filteredRows idxSet support")

    do(
'''    var body = el("div", "dash-tile-body");
    node.a''',
'''    var body = el("div", "dash-tile-body");
    if (tile.type === "indicator" || tile.type === "list" || tile.type === "pivot") {
      LIVE_HOSTS.push({ body: body, tile: tile, page: page });
    }
    node.a''',
        "LIVE_HOSTS tracking")

    do(
'''  var MAP_HOSTS = [];      // {host, tile} for the post-layout init pass''',
'''  var MAP_HOSTS = [];      // {host, tile} for the post-layout init pass
  var LIVE_HOSTS = [];

  function refreshDataTiles(page) {
    LIVE_HOSTS.forEach(function (h) {
      if (h.page !== page) return;
      h.body.innerHTML = "";
      if (h.tile.type === "indicator") renderIndicator(h.body, h.tile, h.page);
      else if (h.tile.type === "list") renderList(h.body, h.tile, h.page);
      else if (h.tile.type === "pivot") renderPivot(h.body, h.tile, h.page);
    });
    CHART_HOSTS.forEach(function (c) {
      if (c.page !== page) return;
      drawChart(c.host, c.tile, c.page);
    });
  }''',
        "LIVE_HOSTS + refreshDataTiles")

    do(
'''  function renderPage(page) {
    CHART_HOSTS = [];
    MAP_INSTANCES.forEach(function (mp) { try { mp.remove(); } catch (e) {} });
    MAP_INSTANCES = [];
    MAP_HOSTS = [];''',
'''  function renderPage(page) {
    CHART_HOSTS = [];
    LIVE_HOSTS = [];
    MAP_INSTANCES.forEach(function (mp) { try { mp.remove(); } catch (e) {} });
    MAP_INSTANCES = [];
    MAP_HOSTS = [];''',
        "renderPage resets LIVE_HOSTS")

    do(
'''    else if (tile.type === "map") renderMap(body, tile);''',
'''    else if (tile.type === "map") renderMap(body, tile, page);''',
        "dispatch passes page to renderMap")

    do(
'''  function renderMap(body, tile) {
    var wrap = el("div", "dash-map-wrap");
    body.appendChild(wrap);
    if (tile.map && typeof L !== "undefined") {
      MAP_HOSTS.push({ host: wrap, tile: tile });''',
'''  function renderMap(body, tile, page) {
    var wrap = el("div", "dash-map-wrap");
    body.appendChild(wrap);
    if (tile.map && typeof L !== "undefined") {
      MAP_HOSTS.push({ host: wrap, tile: tile, page: page });''',
        "renderMap signature")

    do(
'''      MAP_HOSTS.forEach(function (h) { initMap(h.host, h.tile); });''',
'''      MAP_HOSTS.forEach(function (h) { initMap(h.host, h.tile, h.page); });''',
        "initMap call passes page")

    do(
'''  function initMap(host, tile) {
    var m = tile.map || {};''',
'''  function initMap(host, tile, page) {
    var m = tile.map || {};
    var gjLayers = [];''',
        "initMap signature + gjLayers")

    do(
'''      }).addTo(map);
      try {
        var b = gj.getBounds();
        if (b.isValid()) bounds = bounds ? bounds.extend(b) : b;
      } catch (e) {}
    });''',
'''      }).addTo(map);
      gjLayers.push({ gj: gj, layerId: lid });
      try {
        var b = gj.getBounds();
        if (b.isValid()) bounds = bounds ? bounds.extend(b) : b;
      } catch (e) {}
    });''',
        "keep gj layer refs")

    do(
'''    map.invalidateSize();
    MAP_INSTANCES.push(map);
  }''',
'''    map.invalidateSize();
    MAP_INSTANCES.push(map);

    if (page && m.source_filter_mode !== "off" && gjLayers.length) {
      var applyExtentSelection = function () {
        var vb = map.getBounds();
        gjLayers.forEach(function (gl) {
          var idx = new Set();
          gl.gj.eachLayer(function (fl) {
            try {
              var inView = fl.getBounds ? vb.intersects(fl.getBounds())
                          : (fl.getLatLng ? vb.contains(fl.getLatLng()) : false);
              if (inView && fl.feature && fl.feature.properties) {
                idx.add(fl.feature.properties.__idx);
              }
            } catch (e) {}
          });
          var s = selections(page.id);
          s[tile.id] = { key: "extent", pairs: [{ idxSet: idx, layerId: gl.layerId }] };
        });
        refreshDataTiles(page);
      };
      map.on("moveend", applyExtentSelection);
      applyExtentSelection();
    }
  }''',
        "moveend extent-filter wiring")

    return html, applied


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    in_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else in_path.rsplit(".html", 1)[0] + "_enriched.html"

    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    if MARKER in html:
        print("Este archivo ya fue enriquecido antes (marca encontrada). No se reaplican parches.")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Copiado sin cambios a: {out_path}")
        return

    d, start2, end = load_dashboard_json(html)
    if d is None:
        print("No parece un export de QGIS Dashboards (no se encontró dashboard-data). Abortando.")
        sys.exit(1)

    print(f"Analizando: {in_path}")

    # 0) basemap satelital -- se hace primero porque reescribe el bloque JSON completo
    html, d, sat_applied = patch_satellite_basemap(
        html, start2, end, d, ESRI_SATELLITE_URL, ESRI_SATELLITE_ATTRIBUTION
    )
    print(f"  [0/8] Basemap satelital (Esri World Imagery): {'aplicado' if sat_applied else 'no aplicable (no se encontró ningún tile de mapa)'}")

    html, fixed_conn = patch_connections_guard(html)
    print(f"  [1/8] Fix connections array/objeto: {'aplicado' if fixed_conn else 'no aplicable (patrón no encontrado)'}")

    layer_id, field = detect_category_field(d)
    if layer_id and field:
        color_field = detect_explicit_color_field(d, layer_id)
        if color_field:
            color_map = build_color_map_from_field(d, layer_id, field, color_field)
            print(f"  [2/8] Usando color real por feature (campo '{color_field}') en vez de paleta genérica.")
        else:
            color_map = build_color_map(d, layer_id, field)
        html, applied_colors = patch_category_colors_and_legend(html, field, color_map)
        print(f"  [2/8] Colores por categoría ('{field}', {len(color_map)} valores, opacidad {FILL_OPACITY}) + leyenda: "
              f"{', '.join(applied_colors) if applied_colors else 'no aplicable'}")
    else:
        color_map = {}
        print("  [2/8] No se encontró ningún Chart/Category selector con category_field -> se omite este paso.")

    html, applied_extent = patch_extent_filter(html)
    ok = len(applied_extent) >= 10  # all sub-patches expected
    print(f"  [3/8] Filtro por pan/zoom del mapa: {'aplicado (' + str(len(applied_extent)) + '/11 pasos)' if applied_extent else 'no aplicable'}")
    if applied_extent and not ok:
        print(f"        ⚠️  Solo se aplicaron {len(applied_extent)}/11 pasos — revisa manualmente, "
              "puede que el bundle JS de este export difiera del que usamos como referencia.")

    html, map_filter_applied = patch_map_visual_filter(html)
    print(f"  [4/8] El mapa oculta/muestra polígonos según selección: "
          f"{'aplicado' if map_filter_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero)'}")

    # etiquetas: por defecto usa el campo 'id' si existe en la capa detectada arriba
    label_applied = False
    if layer_id and "id" in d["layers"].get(layer_id, {}).get("fields", []):
        html, label_applied = patch_polygon_labels(html, "id")
    print(f"  [5/8] Etiquetas 'id' sobre los polígonos: "
          f"{'aplicado' if label_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero, o que exista el campo id)'}")

    html, controls_applied = patch_map_controls(html)
    print(f"  [6/8] Panel de controles (mapa base + transparencia): "
          f"{'aplicado' if controls_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero)'}")

    html, sort_applied = patch_selector_numeric_sort(html)
    print(f"  [7/8] Orden numérico en el desplegable del Category selector: "
          f"{'aplicado' if sort_applied else 'no aplicable (patrón no encontrado)'}")

    html, ind_color_applied = patch_indicator_style_color(html)
    print(f"  [8/8] Indicator respeta el color configurado en QGIS (value_color): "
          f"{'aplicado' if ind_color_applied else 'no aplicable (patrón no encontrado)'}")

    # insert idempotency marker right after the dashboard-data script closes
    marker_anchor = '</script>\n<script>'
    if marker_anchor in html:
        html = html.replace(marker_anchor, f'</script>\n<script>{MARKER}\n', 1)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Listo. Archivo enriquecido guardado en: {out_path}")


if __name__ == "__main__":
    main()#!/usr/bin/env python3
"""
enrich_dashboard.py — aplica automáticamente a cualquier export del plugin
QGIS Dashboards los 4 arreglos/mejoras que fuimos construyendo a mano:

  1. FIX  connections mixto array/objeto -> TypeError que deja la página en blanco.
  2. MEJORA colores por categoría en el mapa + leyenda funcional (usa el color
     real por feature si existe un campo tipo codigo_color; si no, genera
     una paleta automática).
  3. MEJORA filtro por extensión: Chart/List/Indicator/Pivot se recalculan al
     hacer pan/zoom en el mapa, sin reconstruir el mapa (no pierdes tu posición).
  4. MEJORA el propio mapa oculta/muestra polígonos según la selección activa
     (Category selector, clic en una barra del Chart, etc.) -- antes el mapa
     siempre dibujaba todos los polígonos sin importar el filtro.

Uso:
    python3 enrich_dashboard.py entrada.html [salida.html]

Es idempotente: si corres el script dos veces sobre el mismo archivo ya
enriquecido, detecta la marca y no vuelve a aplicar los parches.
"""
import sys
import json
import re

MARKER = "/* DASH_ENRICHED_V1 */"

FILL_OPACITY = 0.55  # antes 0.75 -- más transparencia para ver mejor el satelital de fondo

LEGEND_TEXT_COLOR = "rgb(255,205,0)"  # amarillo/dorado oficial Terrasos -- título y etiquetas de la leyenda

ESRI_SATELLITE_URL = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
ESRI_SATELLITE_ATTRIBUTION = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community"

QUALITATIVE_PALETTE = [
    "rgb(215,25,28)", "rgb(43,131,186)", "rgb(230,200,60)",
    "rgb(26,150,65)", "rgb(148,103,189)", "rgb(255,127,0)",
    "rgb(23,190,207)", "rgb(227,119,194)",
]


def load_dashboard_json(html):
    start = html.find('<script type="application/json" id="dashboard-data">')
    if start == -1:
        return None, None, None
    start2 = html.find("\n", start) + 1
    end = html.find("</script>", start2)
    data_text = html[start2:end]
    return json.loads(data_text), start2, end


def detect_category_field(d):
    """Look through tiles for a chart/category_selector that names a category_field
    and layer_id, so we know which field/layer to color by."""
    for page in d.get("pages", []):
        for t in page.get("tiles", []):
            cfg = t.get("config", {})
            field = cfg.get("category_field")
            layer_id = t.get("layer_id") or cfg.get("layer_id")
            if field and layer_id and layer_id in d.get("layers", {}):
                return layer_id, field
    return None, None


COLOR_FIELD_CANDIDATES = ["codigo_color", "color", "hex_color", "map_color"]


def detect_explicit_color_field(d, layer_id):
    """If the layer already carries a per-feature hex color computed in SQL
    (e.g. codigo_color), prefer that over generating a palette — it's exact,
    not guessed, and stays correct even if categories/order change later."""
    layer = d["layers"].get(layer_id, {})
    fields = layer.get("fields", [])
    for cand in COLOR_FIELD_CANDIDATES:
        if cand in fields:
            return cand
    return None


def natural_key(s):
    """Ordena '1. ÓPTIMA...', '2. ALTA...' ... numéricamente por el prefijo,
    en vez de alfabéticamente (que pondría '1.' antes de '10.' pero después
    de cualquier letra, y mezclaría el orden como vimos en la leyenda)."""
    s = str(s)
    m = re.match(r'^\s*(\d+)', s)
    if m:
        return (0, int(m.group(1)), s)
    return (1, 0, s)


def build_color_map_from_field(d, layer_id, category_field, color_field):
    """Build {category_value: hex_color} by reading the real, already-computed
    color per feature straight from the data — no palette guessing. Se ordena
    con natural_key así el diccionario (y por lo tanto la leyenda, que itera
    Object.keys() en orden de inserción) sale en orden lógico, no en el orden
    en que las categorías aparecieron primero en los datos."""
    layer = d["layers"][layer_id]
    raw = {}
    for feat in layer.get("features", []):
        cat = feat.get(category_field)
        col = feat.get(color_field)
        if cat is not None and col and cat not in raw:
            raw[cat] = col
    return {k: raw[k] for k in sorted(raw.keys(), key=natural_key)}


def build_color_map(d, layer_id, field):
    layer = d["layers"][layer_id]
    values = []
    seen = set()
    for feat in layer.get("features", []):
        v = feat.get(field)
        if v is not None and v not in seen:
            seen.add(v)
            values.append(v)
    values.sort(key=natural_key)
    return {v: QUALITATIVE_PALETTE[i % len(QUALITATIVE_PALETTE)] for i, v in enumerate(values)}


def js_str(s):
    return json.dumps(s, ensure_ascii=False)


def patch_connections_guard(html):
    old = "if (conns[src].indexOf(tile.id) >= 0) {"
    new = "if (Array.isArray(conns[src]) && conns[src].indexOf(tile.id) >= 0) {"
    if old in html:
        return html.replace(old, new, 1), True
    return html, False


def patch_category_colors_and_legend(html, field, color_map):
    applied = []

    color_js = "{\n" + ",\n".join(
        f"        {js_str(k)}: {js_str(v)}" for k, v in color_map.items()
    ) + "\n      }"

    # 0) make the category colors GLOBAL (not just local to the map's style
    # function), and add a catColor() helper, so the Chart widget (bars, pie,
    # lollipop, dot, funnel, treemap...) can use the exact same colors as the
    # map instead of the plugin's own generic/unordered palette.
    old_anchor = 'function color(i) { return SERIES[i % SERIES.length]; }'
    new_anchor = (
        old_anchor + '\n'
        f'  var DASH_CATEGORY_FIELD = {js_str(field)};\n'
        f'  var DASH_CATEGORY_COLORS = {color_js};\n'
        '  function catColor(cat, i) {\n'
        '    var c = DASH_CATEGORY_COLORS ? DASH_CATEGORY_COLORS[cat] : null;\n'
        '    return c || color(i);\n'
        '  }'
    )
    if old_anchor in html and html.count(old_anchor) == 1:
        html = html.replace(old_anchor, new_anchor, 1)
        applied.append("global category colors + catColor()")

        # every chart painter that colors bars/points/segments by category and
        # already distinguishes the selected one uses this exact pattern
        old_sel = '(d[0] === selKey) ? "var(--muted)" : color(i);'
        new_sel = '(d[0] === selKey) ? "var(--muted)" : catColor(d[0], i);'
        n_sel = html.count(old_sel)
        if n_sel:
            html = html.replace(old_sel, new_sel)
            applied.append(f"chart bars/points recolored ({n_sel} painters)")

        # pie/donut: segment fill + legend swatch
        old_pie1 = 'fill: color(i), stroke: "#ffffff", "stroke-width": 1, cursor: "pointer" });'
        new_pie1 = 'fill: catColor(d[0], i), stroke: "#ffffff", "stroke-width": 1, cursor: "pointer" });'
        if old_pie1 in html and html.count(old_pie1) == 1:
            html = html.replace(old_pie1, new_pie1, 1)
            applied.append("pie/donut segments recolored")

        old_pie2 = 'fill: color(i) }));'
        new_pie2 = 'fill: catColor(d[0], i) }));'
        if old_pie2 in html and html.count(old_pie2) == 1:
            html = html.replace(old_pie2, new_pie2, 1)
            applied.append("pie/donut legend swatches recolored")

    old_style = (
        '      var col = color(idx);\n'
        '      var gj = L.geoJSON(fc, {\n'
        '        style: function () {\n'
        '          return { color: col, weight: 2, fillColor: col, fillOpacity: 0.25 };\n'
        '        },'
    )
    new_style = (
        '      var col = color(idx);\n'
        '      var gj = L.geoJSON(fc, {\n'
        '        style: function (f) {\n'
        '          var val = f && f.properties ? f.properties[DASH_CATEGORY_FIELD] : null;\n'
        '          var c = (DASH_CATEGORY_COLORS && DASH_CATEGORY_COLORS[val]) || col;\n'
        '          return { color: "#f7f7f7", weight: 1, fillColor: c, fillOpacity: ' + str(FILL_OPACITY) + ' };\n'
        '        },'
    )
    if old_style in html:
        html = html.replace(old_style, new_style, 1)
        applied.append("map category colors")

    old_dispatch = (
        '    } else {\n'
        '      body.appendChild(el("div", "dash-note", tile.type));\n'
        '    }\n'
        '    return node;\n'
        '  }'
    )
    legend_fn = (
        '  function renderLegend(body, tile) {\n'
        '    var wrap = el("div", "dash-legend");\n'
        '    wrap.style.padding = "8px";\n'
        '    wrap.style.fontSize = "12px";\n'
        '    var title = el("div", "dash-legend-title", (tile.config && tile.config.title) || '
        + js_str(field) + ');\n'
        '    title.style.fontWeight = "700";\n'
        '    title.style.marginBottom = "6px";\n'
        f'    title.style.color = {js_str(LEGEND_TEXT_COLOR)};\n'
        '    wrap.appendChild(title);\n'
        '    Object.keys(DASH_CATEGORY_COLORS || {}).forEach(function (label) {\n'
        '      var row = el("div", "dash-legend-row");\n'
        '      row.style.display = "flex";\n'
        '      row.style.alignItems = "center";\n'
        '      row.style.gap = "8px";\n'
        '      row.style.marginBottom = "4px";\n'
        '      var swatch = el("span", "dash-legend-swatch");\n'
        '      swatch.style.display = "inline-block";\n'
        '      swatch.style.width = "14px";\n'
        '      swatch.style.height = "14px";\n'
        '      swatch.style.background = DASH_CATEGORY_COLORS[label];\n'
        '      swatch.style.border = "1px solid #ccc";\n'
        '      var text = el("span", "dash-legend-label", label);\n'
        f'      text.style.color = {js_str(LEGEND_TEXT_COLOR)};\n'
        '      row.appendChild(swatch);\n'
        '      row.appendChild(text);\n'
        '      wrap.appendChild(row);\n'
        '    });\n'
        '    body.appendChild(wrap);\n'
        '  }'
    )
    new_dispatch = (
        '    } else if (tile.type === "legend") {\n'
        '      renderLegend(body, tile);\n'
        '    } else {\n'
        '      body.appendChild(el("div", "dash-note", tile.type));\n'
        '    }\n'
        '    return node;\n'
        '  }\n\n' + legend_fn
    )
    if old_dispatch in html:
        html = html.replace(old_dispatch, new_dispatch, 1)
        applied.append("legend widget")

    return html, applied


def patch_indicator_style_color(html):
    """El plugin SÍ guarda el color que configuras en QGIS para el Indicator
    (cfg.style.value_color), pero el runtime del export lo ignora y usa el
    color de acento del tema (var(--accent)) para todos los indicadores por
    igual. Este parche hace que respete el color que de verdad configuraste."""
    old = (
        '    var valNode = el("div", "dash-ind-value");\n'
        '    if (cfg.value_size) valNode.style.fontSize = cfg.value_size + "px";\n'
        '    wrap.appendChild(valNode);'
    )
    new = (
        '    var valNode = el("div", "dash-ind-value");\n'
        '    if (cfg.value_size) valNode.style.fontSize = cfg.value_size + "px";\n'
        '    if (cfg.style && cfg.style.value_color) valNode.style.color = cfg.style.value_color;\n'
        '    wrap.appendChild(valNode);'
    )
    if old not in html or html.count(old) != 1:
        return html, False
    html = html.replace(old, new, 1)
    return html, True


def patch_polygon_labels(html, label_field, min_zoom=13):
    """Agrega etiquetas de texto sobre cada polígono del mapa, usando el campo
    indicado -- pero de forma PEREZOSA: solo crea los tooltips en el DOM
    cuando el zoom ya es suficiente para verlos, y los destruye al alejar el
    zoom. Crear miles de tooltips permanentes de una sola vez al cargar (lo
    que hacía la versión anterior) congela el navegador con capas grandes."""

    old_decl = 'var m = tile.map || {};'
    if html.count(old_decl) != 1:
        return html, False
    html = html.replace(old_decl, old_decl + '\n    var LABEL_LAYERS = [];', 1)

    old = (
        '        onEachFeature: function (f, lyr) {\n'
        '          lyr.bindPopup(identifyHtml(layer.fields, f.properties));\n'
        '        }\n'
        '      }).addTo(map);'
    )
    new = (
        '        onEachFeature: function (f, lyr) {\n'
        '          lyr.bindPopup(identifyHtml(layer.fields, f.properties));\n'
        f'          var labelVal = f.properties ? f.properties[{js_str(label_field)}] : null;\n'
        '          if (labelVal !== null && labelVal !== undefined && labelVal !== "") {\n'
        '            lyr._dashLabel = String(labelVal);\n'  # cheap: no DOM work yet
        '          }\n'
        '        }\n'
        '      }).addTo(map);\n'
        '      LABEL_LAYERS.push(gj);'
    )
    if old not in html:
        return html, False
    html = html.replace(old, new, 1)

    old2 = '    var ext = m.extent;'
    if html.count(old2) != 1:
        return html, False
    new2 = (
        '    if (!document.getElementById("dash-poly-label-style")) {\n'
        '      var lst = document.createElement("style");\n'
        '      lst.id = "dash-poly-label-style";\n'
        '      lst.textContent = ".dash-poly-label{background:transparent;border:none;'
        'box-shadow:none;font-size:11px;font-weight:600;color:#222;'
        'text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;}";\n'
        '      document.head.appendChild(lst);\n'
        '    }\n'
        f'    var LABEL_MIN_ZOOM = {min_zoom};\n'
        '    var updateLabelVisibility = function () {\n'
        '      var show = map.getZoom() >= LABEL_MIN_ZOOM;\n'
        '      var vb = show ? map.getBounds() : null;\n'
        '      LABEL_LAYERS.forEach(function (glGroup) {\n'
        '        glGroup.eachLayer(function (lyr) {\n'
        '          if (!lyr._dashLabel) return;\n'
        '          if (show) {\n'
        '            var lb = lyr.getBounds ? lyr.getBounds() : null;\n'
        '            var inView = !lb || vb.intersects(lb);\n'
        '            if (inView && !lyr.getTooltip()) {\n'
        '              lyr.bindTooltip(lyr._dashLabel, {\n'
        '                permanent: true, direction: "center", className: "dash-poly-label"\n'
        '              });\n'
        '            } else if (!inView && lyr.getTooltip()) {\n'
        '              lyr.unbindTooltip();\n'
        '            }\n'
        '          } else if (lyr.getTooltip()) {\n'
        '            lyr.unbindTooltip();\n'
        '          }\n'
        '        });\n'
        '      });\n'
        '    };\n'
        '    map.on("zoomend moveend", updateLabelVisibility);\n'
        '    updateLabelVisibility();\n'
        '    var ext = m.extent;'
    )
    html = html.replace(old2, new2, 1)
    return html, True


def patch_satellite_basemap(html, start2, end, d, url_template, attribution, max_zoom=19):
    """Cambia el basemap de todos los tiles de mapa a imágenes satelitales
    (o cualquier tile server que le pases), editando directamente el JSON
    embebido -- no hace falta tocar el runtime JS para esto."""
    changed = False
    for page in d.get("pages", []):
        for t in page.get("tiles", []):
            if t.get("type") == "map":
                m = t.setdefault("map", {})
                bm = m.setdefault("basemap", {})
                bm["url_template"] = url_template
                bm["attribution"] = attribution
                bm["max_zoom"] = max_zoom
                changed = True
    if not changed:
        return html, d, False
    new_json = json.dumps(d, ensure_ascii=False)
    html = html[:start2] + new_json + html[end:]
    return html, d, True


def patch_selector_numeric_sort(html):
    """El <select> del Category selector ordena las opciones con .sort() por
    defecto, que es orden de TEXTO (1, 12, 13, 2, 3...). Lo cambia a un orden
    natural: numérico cuando el valor es un número, alfabético si no."""
    old = 'Object.keys(seen).sort().forEach(function (v) { select.appendChild(new Option(v, v)); });'
    new = (
        'Object.keys(seen).sort(function (a, b) {\n'
        '        var na = Number(a), nb = Number(b);\n'
        '        var an = a !== "" && !isNaN(na), bn = b !== "" && !isNaN(nb);\n'
        '        if (an && bn) return na - nb;\n'
        '        return a < b ? -1 : (a > b ? 1 : 0);\n'
        '      }).forEach(function (v) { select.appendChild(new Option(v, v)); });'
    )
    if old not in html or html.count(old) != 1:
        return html, False
    html = html.replace(old, new, 1)
    return html, True


def patch_map_visual_filter(html):
    """Hoy el mapa dibuja SIEMPRE todos los polígonos, sin importar qué haya
    seleccionado un Category selector o un clic en el Chart -- solo esos otros
    widgets se recalculan. Este parche hace que el propio mapa oculte/muestre
    polígonos según las selecciones activas que lo tengan como destino."""
    old_anchor = 'var MAP_INSTANCES = [];  // live L.map objects, torn down on page switch'
    new_anchor = old_anchor + '''

  function mapPredsFor(tileId, page) {
    if (!page) return [];
    var conns = page.connections || {};
    var sel = selections(page.id);
    var preds = [];
    Object.keys(conns).forEach(function (src) {
      if (Array.isArray(conns[src]) && conns[src].indexOf(tileId) >= 0) {
        var s = sel[src];
        if (s && s.pairs && s.pairs.length) preds = preds.concat(s.pairs);
      }
    });
    return preds;
  }

  function applyMapPreds(layer, preds) {
    if (!preds.length) return layer;
    var feats = [], geoms = [];
    (layer.features || []).forEach(function (r, i) {
      var ok = preds.every(function (p) {
        if (p.idxSet) return true;
        if (p.lo !== undefined) {
          var n = parseFloat(r[p.field]);
          return !isNaN(n) && n >= p.lo && n < p.hi;
        }
        return String(r[p.field]) === String(p.value);
      });
      if (ok) { feats.push(r); geoms.push((layer.geometry || [])[i]); }
    });
    return { features: feats, geometry: geoms, fields: layer.fields };
  }'''
    if html.count(old_anchor) != 1:
        return html, False
    html = html.replace(old_anchor, new_anchor, 1)

    old_fc = '''      var layer = DATA.layers[lid];
      if (!layer || !layer.geometry) return;
      var fc = featureCollection(layer);'''
    new_fc = '''      var layer = DATA.layers[lid];
      if (!layer || !layer.geometry) return;
      var preds = mapPredsFor(tile.id, page);
      var fc = featureCollection(applyMapPreds(layer, preds));'''
    if html.count(old_fc) != 1:
        return html, False
    html = html.replace(old_fc, new_fc, 1)
    return html, True


def patch_map_controls(html):
    """Agrega un panel flotante sobre el mapa con: (1) selector de mapa base
    (Satelital/Calles/Claro/Topográfico) y (2) control deslizante de
    transparencia de los polígonos."""
    old_tile = '''    L.tileLayer(bm.url_template ||
      "https://tile.openstreetmap.org/{z}/{x}/{y}.png", opts).addTo(map);'''
    new_tile = '''    var baseLayer = L.tileLayer(bm.url_template ||
      "https://tile.openstreetmap.org/{z}/{x}/{y}.png", opts).addTo(map);'''
    if html.count(old_tile) != 1:
        return html, False
    html = html.replace(old_tile, new_tile, 1)

    old_anchor = '    var ext = m.extent;'
    if html.count(old_anchor) != 1:
        return html, False
    new_anchor = '''    (function () {
      var BASEMAP_OPTIONS = [
        { label: "Satelital (Esri)", url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", attribution: "Tiles &copy; Esri" },
        { label: "Calles (OSM)", url: "https://tile.openstreetmap.org/{z}/{x}/{y}.png", attribution: "&copy; OpenStreetMap contributors" },
        { label: "Claro (Carto)", url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png", attribution: "&copy; OpenStreetMap contributors &copy; CARTO" },
        { label: "Topogr\\u00e1fico (Esri)", url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}", attribution: "Tiles &copy; Esri" }
      ];
      if (!document.getElementById("dash-map-controls-style")) {
        var st = document.createElement("style");
        st.id = "dash-map-controls-style";
        st.textContent = ".dash-map-controls{position:absolute;top:8px;right:8px;z-index:1000;" +
          "background:rgba(255,255,255,.92);border:1px solid #d7dbe0;border-radius:8px;padding:8px 10px;" +
          "font-size:11px;font-family:inherit;box-shadow:0 1px 4px rgba(0,0,0,.15);display:flex;flex-direction:column;gap:6px;min-width:150px;}" +
          ".dash-map-controls label{font-weight:600;color:#333;margin-bottom:2px;display:block;}" +
          ".dash-map-controls select,.dash-map-controls input[type=range]{width:100%;box-sizing:border-box;}";
        document.head.appendChild(st);
      }
      var panel = document.createElement("div");
      panel.className = "dash-map-controls";
      panel.innerHTML =
        '<div><label>Mapa base</label><select class="dash-basemap-select"></select></div>' +
        '<div><label>Transparencia</label><input type="range" class="dash-opacity-range" min="0" max="100" value="''' + str(int(FILL_OPACITY * 100)) + '''"></div>';
      host.appendChild(panel);

      var sel = panel.querySelector(".dash-basemap-select");
      BASEMAP_OPTIONS.forEach(function (opt, oi) {
        var o = document.createElement("option");
        o.value = String(oi);
        o.textContent = opt.label;
        if (opt.url === (bm.url_template || "")) o.selected = true;
        sel.appendChild(o);
      });
      sel.addEventListener("change", function () {
        var opt = BASEMAP_OPTIONS[Number(sel.value)];
        if (!opt) return;
        map.removeLayer(baseLayer);
        baseLayer = L.tileLayer(opt.url, { maxZoom: bm.max_zoom || 19, attribution: opt.attribution }).addTo(map);
        baseLayer.bringToBack();
      });

      var range = panel.querySelector(".dash-opacity-range");
      range.addEventListener("input", function () {
        var v = Number(range.value) / 100;
        gjLayers.forEach(function (gl) { gl.gj.setStyle({ fillOpacity: v }); });
      });
    })();

    var ext = m.extent;'''
    html = html.replace(old_anchor, new_anchor, 1)
    return html, True


def patch_extent_filter(html):
    applied = []

    def do(old, new, label):
        nonlocal html
        if old in html:
            html = html.replace(old, new, 1)
            applied.append(label)
            return True
        return False

    do(
'''  function featureCollection(layer) {
    var rows = layer.features || [];
    var geoms = layer.geometry || [];
    var feats = [];
    for (var i = 0; i < rows.length; i++) {
      if (!geoms[i]) continue;
      feats.push({ type: "Feature", geometry: geoms[i], properties: rows[i] });
    }
    return { type: "FeatureCollection", features: feats };
  }''',
'''  function featureCollection(layer) {
    var rows = layer.features || [];
    var geoms = layer.geometry || [];
    var feats = [];
    for (var i = 0; i < rows.length; i++) {
      if (!geoms[i]) continue;
      if (rows[i].__idx === undefined) rows[i].__idx = i;
      feats.push({ type: "Feature", geometry: geoms[i], properties: rows[i] });
    }
    return { type: "FeatureCollection", features: feats };
  }''',
        "row index tagging")

    do(
'''    return rows.filter(function (r) {
      return preds.every(function (p) {
        if (p.lo !== undefined) {          // numeric range predicate (histogram)
          var n = parseFloat(r[p.field]);
          return !isNaN(n) && n >= p.lo && n < p.hi;
        }
        return eq(r[p.field], p.value);
      });
    });
  }''',
'''    return rows.filter(function (r) {
      return preds.every(function (p) {
        if (p.idxSet) {
          if (p.layerId && tile.layer_id && p.layerId !== tile.layer_id) return true;
          return p.idxSet.has(r.__idx);
        }
        if (p.lo !== undefined) {          // numeric range predicate (histogram)
          var n = parseFloat(r[p.field]);
          return !isNaN(n) && n >= p.lo && n < p.hi;
        }
        return eq(r[p.field], p.value);
      });
    });
  }''',
        "filteredRows idxSet support")

    do(
'''    var body = el("div", "dash-tile-body");
    node.a''',
'''    var body = el("div", "dash-tile-body");
    if (tile.type === "indicator" || tile.type === "list" || tile.type === "pivot") {
      LIVE_HOSTS.push({ body: body, tile: tile, page: page });
    }
    node.a''',
        "LIVE_HOSTS tracking")

    do(
'''  var MAP_HOSTS = [];      // {host, tile} for the post-layout init pass''',
'''  var MAP_HOSTS = [];      // {host, tile} for the post-layout init pass
  var LIVE_HOSTS = [];

  function refreshDataTiles(page) {
    LIVE_HOSTS.forEach(function (h) {
      if (h.page !== page) return;
      h.body.innerHTML = "";
      if (h.tile.type === "indicator") renderIndicator(h.body, h.tile, h.page);
      else if (h.tile.type === "list") renderList(h.body, h.tile, h.page);
      else if (h.tile.type === "pivot") renderPivot(h.body, h.tile, h.page);
    });
    CHART_HOSTS.forEach(function (c) {
      if (c.page !== page) return;
      drawChart(c.host, c.tile, c.page);
    });
  }''',
        "LIVE_HOSTS + refreshDataTiles")

    do(
'''  function renderPage(page) {
    CHART_HOSTS = [];
    MAP_INSTANCES.forEach(function (mp) { try { mp.remove(); } catch (e) {} });
    MAP_INSTANCES = [];
    MAP_HOSTS = [];''',
'''  function renderPage(page) {
    CHART_HOSTS = [];
    LIVE_HOSTS = [];
    MAP_INSTANCES.forEach(function (mp) { try { mp.remove(); } catch (e) {} });
    MAP_INSTANCES = [];
    MAP_HOSTS = [];''',
        "renderPage resets LIVE_HOSTS")

    do(
'''    else if (tile.type === "map") renderMap(body, tile);''',
'''    else if (tile.type === "map") renderMap(body, tile, page);''',
        "dispatch passes page to renderMap")

    do(
'''  function renderMap(body, tile) {
    var wrap = el("div", "dash-map-wrap");
    body.appendChild(wrap);
    if (tile.map && typeof L !== "undefined") {
      MAP_HOSTS.push({ host: wrap, tile: tile });''',
'''  function renderMap(body, tile, page) {
    var wrap = el("div", "dash-map-wrap");
    body.appendChild(wrap);
    if (tile.map && typeof L !== "undefined") {
      MAP_HOSTS.push({ host: wrap, tile: tile, page: page });''',
        "renderMap signature")

    do(
'''      MAP_HOSTS.forEach(function (h) { initMap(h.host, h.tile); });''',
'''      MAP_HOSTS.forEach(function (h) { initMap(h.host, h.tile, h.page); });''',
        "initMap call passes page")

    do(
'''  function initMap(host, tile) {
    var m = tile.map || {};''',
'''  function initMap(host, tile, page) {
    var m = tile.map || {};
    var gjLayers = [];''',
        "initMap signature + gjLayers")

    do(
'''      }).addTo(map);
      try {
        var b = gj.getBounds();
        if (b.isValid()) bounds = bounds ? bounds.extend(b) : b;
      } catch (e) {}
    });''',
'''      }).addTo(map);
      gjLayers.push({ gj: gj, layerId: lid });
      try {
        var b = gj.getBounds();
        if (b.isValid()) bounds = bounds ? bounds.extend(b) : b;
      } catch (e) {}
    });''',
        "keep gj layer refs")

    do(
'''    map.invalidateSize();
    MAP_INSTANCES.push(map);
  }''',
'''    map.invalidateSize();
    MAP_INSTANCES.push(map);

    if (page && m.source_filter_mode !== "off" && gjLayers.length) {
      var applyExtentSelection = function () {
        var vb = map.getBounds();
        gjLayers.forEach(function (gl) {
          var idx = new Set();
          gl.gj.eachLayer(function (fl) {
            try {
              var inView = fl.getBounds ? vb.intersects(fl.getBounds())
                          : (fl.getLatLng ? vb.contains(fl.getLatLng()) : false);
              if (inView && fl.feature && fl.feature.properties) {
                idx.add(fl.feature.properties.__idx);
              }
            } catch (e) {}
          });
          var s = selections(page.id);
          s[tile.id] = { key: "extent", pairs: [{ idxSet: idx, layerId: gl.layerId }] };
        });
        refreshDataTiles(page);
      };
      map.on("moveend", applyExtentSelection);
      applyExtentSelection();
    }
  }''',
        "moveend extent-filter wiring")

    return html, applied


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    in_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else in_path.rsplit(".html", 1)[0] + "_enriched.html"

    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    if MARKER in html:
        print("Este archivo ya fue enriquecido antes (marca encontrada). No se reaplican parches.")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Copiado sin cambios a: {out_path}")
        return

    d, start2, end = load_dashboard_json(html)
    if d is None:
        print("No parece un export de QGIS Dashboards (no se encontró dashboard-data). Abortando.")
        sys.exit(1)

    print(f"Analizando: {in_path}")

    # 0) basemap satelital -- se hace primero porque reescribe el bloque JSON completo
    html, d, sat_applied = patch_satellite_basemap(
        html, start2, end, d, ESRI_SATELLITE_URL, ESRI_SATELLITE_ATTRIBUTION
    )
    print(f"  [0/8] Basemap satelital (Esri World Imagery): {'aplicado' if sat_applied else 'no aplicable (no se encontró ningún tile de mapa)'}")

    html, fixed_conn = patch_connections_guard(html)
    print(f"  [1/8] Fix connections array/objeto: {'aplicado' if fixed_conn else 'no aplicable (patrón no encontrado)'}")

    layer_id, field = detect_category_field(d)
    if layer_id and field:
        color_field = detect_explicit_color_field(d, layer_id)
        if color_field:
            color_map = build_color_map_from_field(d, layer_id, field, color_field)
            print(f"  [2/8] Usando color real por feature (campo '{color_field}') en vez de paleta genérica.")
        else:
            color_map = build_color_map(d, layer_id, field)
        html, applied_colors = patch_category_colors_and_legend(html, field, color_map)
        print(f"  [2/8] Colores por categoría ('{field}', {len(color_map)} valores, opacidad {FILL_OPACITY}) + leyenda: "
              f"{', '.join(applied_colors) if applied_colors else 'no aplicable'}")
    else:
        color_map = {}
        print("  [2/8] No se encontró ningún Chart/Category selector con category_field -> se omite este paso.")

    html, applied_extent = patch_extent_filter(html)
    ok = len(applied_extent) >= 10  # all sub-patches expected
    print(f"  [3/8] Filtro por pan/zoom del mapa: {'aplicado (' + str(len(applied_extent)) + '/11 pasos)' if applied_extent else 'no aplicable'}")
    if applied_extent and not ok:
        print(f"        ⚠️  Solo se aplicaron {len(applied_extent)}/11 pasos — revisa manualmente, "
              "puede que el bundle JS de este export difiera del que usamos como referencia.")

    html, map_filter_applied = patch_map_visual_filter(html)
    print(f"  [4/8] El mapa oculta/muestra polígonos según selección: "
          f"{'aplicado' if map_filter_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero)'}")

    # etiquetas: por defecto usa el campo 'id' si existe en la capa detectada arriba
    label_applied = False
    if layer_id and "id" in d["layers"].get(layer_id, {}).get("fields", []):
        html, label_applied = patch_polygon_labels(html, "id")
    print(f"  [5/8] Etiquetas 'id' sobre los polígonos: "
          f"{'aplicado' if label_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero, o que exista el campo id)'}")

    html, controls_applied = patch_map_controls(html)
    print(f"  [6/8] Panel de controles (mapa base + transparencia): "
          f"{'aplicado' if controls_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero)'}")

    html, sort_applied = patch_selector_numeric_sort(html)
    print(f"  [7/8] Orden numérico en el desplegable del Category selector: "
          f"{'aplicado' if sort_applied else 'no aplicable (patrón no encontrado)'}")

    html, ind_color_applied = patch_indicator_style_color(html)
    print(f"  [8/8] Indicator respeta el color configurado en QGIS (value_color): "
          f"{'aplicado' if ind_color_applied else 'no aplicable (patrón no encontrado)'}")

    # insert idempotency marker right after the dashboard-data script closes
    marker_anchor = '</script>\n<script>'
    if marker_anchor in html:
        html = html.replace(marker_anchor, f'</script>\n<script>{MARKER}\n', 1)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Listo. Archivo enriquecido guardado en: {out_path}")


if __name__ == "__main__":
    main()
