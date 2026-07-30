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


def build_color_map_from_field(d, layer_id, category_field, color_field):
    """Build {category_value: hex_color} by reading the real, already-computed
    color per feature straight from the data — no palette guessing."""
    layer = d["layers"][layer_id]
    color_map = {}
    for feat in layer.get("features", []):
        cat = feat.get(category_field)
        col = feat.get(color_field)
        if cat is not None and col and cat not in color_map:
            color_map[cat] = col
    return color_map


def build_color_map(d, layer_id, field):
    layer = d["layers"][layer_id]
    values = []
    seen = set()
    for feat in layer.get("features", []):
        v = feat.get(field)
        if v is not None and v not in seen:
            seen.add(v)
            values.append(v)
    values.sort(key=lambda x: str(x))
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
        '          return { color: "#f7f7f7", weight: 1, fillColor: c, fillOpacity: 0.75 };\n'
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


def patch_polygon_labels(html, label_field, min_zoom=13):
    """Agrega etiquetas de texto (permanentes, centradas) sobre cada polígono
    del mapa, usando el campo indicado. Se ocultan automáticamente por debajo
    de min_zoom para evitar amontonamiento cuando el mapa está muy alejado."""
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
        '            lyr.bindTooltip(String(labelVal), {\n'
        '              permanent: true, direction: "center", className: "dash-poly-label"\n'
        '            });\n'
        '          }\n'
        '        }\n'
        '      }).addTo(map);'
    )
    if old not in html:
        return html, False

    html = html.replace(old, new, 1)

    # zoom-based show/hide + minimal CSS, injected once right before fitBounds/invalidateSize
    old2 = (
        '    map.invalidateSize();\n'
        '    MAP_INSTANCES.push(map);\n'
        '  }'
    )
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
        '      gjLayers.forEach(function (gl) {\n'
        '        gl.gj.eachLayer(function (lyr) {\n'
        '          if (!lyr.getTooltip || !lyr.getTooltip()) return;\n'
        '          if (show && !lyr.isTooltipOpen()) lyr.openTooltip();\n'
        '          else if (!show && lyr.isTooltipOpen()) lyr.closeTooltip();\n'
        '        });\n'
        '      });\n'
        '    };\n'
        '    map.on("zoomend", updateLabelVisibility);\n'
        '    updateLabelVisibility();\n'
        '    map.invalidateSize();\n'
        '    MAP_INSTANCES.push(map);\n'
        '  }'
    )
    if old2 not in html:
        return html, False
    html = html.replace(old2, new2, 1)
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

    d, _, _ = load_dashboard_json(html)
    if d is None:
        print("No parece un export de QGIS Dashboards (no se encontró dashboard-data). Abortando.")
        sys.exit(1)

    print(f"Analizando: {in_path}")

    html, fixed_conn = patch_connections_guard(html)
    print(f"  [1/3] Fix connections array/objeto: {'aplicado' if fixed_conn else 'no aplicable (patrón no encontrado)'}")

    layer_id, field = detect_category_field(d)
    if layer_id and field:
        color_field = detect_explicit_color_field(d, layer_id)
        if color_field:
            color_map = build_color_map_from_field(d, layer_id, field, color_field)
            print(f"  [2/3] Usando color real por feature (campo '{color_field}') en vez de paleta genérica.")
        else:
            color_map = build_color_map(d, layer_id, field)
        html, applied_colors = patch_category_colors_and_legend(html, field, color_map)
        print(f"  [2/3] Colores por categoría ('{field}', {len(color_map)} valores) + leyenda: "
              f"{', '.join(applied_colors) if applied_colors else 'no aplicable'}")
    else:
        print("  [2/3] No se encontró ningún Chart/Category selector con category_field -> se omite este paso.")

    html, applied_extent = patch_extent_filter(html)
    ok = len(applied_extent) >= 10  # all sub-patches expected
    print(f"  [3/4] Filtro por pan/zoom del mapa: {'aplicado (' + str(len(applied_extent)) + '/11 pasos)' if applied_extent else 'no aplicable'}")
    if applied_extent and not ok:
        print(f"        ⚠️  Solo se aplicaron {len(applied_extent)}/11 pasos — revisa manualmente, "
              "puede que el bundle JS de este export difiera del que usamos como referencia.")

    html, map_filter_applied = patch_map_visual_filter(html)
    print(f"  [4/4] El mapa oculta/muestra polígonos según selección (Category selector, clic en Chart, etc.): "
          f"{'aplicado' if map_filter_applied else 'no aplicable (revisa que el paso 3 se haya aplicado primero)'}")

    # insert idempotency marker right after the dashboard-data script closes
    marker_anchor = '</script>\n<script>'
    if marker_anchor in html:
        html = html.replace(marker_anchor, f'</script>\n<script>{MARKER}\n', 1)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Listo. Archivo enriquecido guardado en: {out_path}")


if __name__ == "__main__":
    main()
