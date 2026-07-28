#!/usr/bin/env python3
"""
enrich_dashboard.py — aplica automáticamente a cualquier export del plugin
QGIS Dashboards los 3 arreglos/mejoras que fuimos construyendo a mano:

  1. FIX  connections mixto array/objeto -> TypeError que deja la página en blanco.
  2. MEJORA colores por categoría en el mapa + leyenda funcional
     (se auto-detecta el campo de categoría desde la config del primer Chart
     o Category selector que encuentre; si no hay ninguno, se omite este paso).
  3. MEJORA filtro por extensión: Chart/List/Indicator/Pivot se recalculan al
     hacer pan/zoom en el mapa, sin reconstruir el mapa (no pierdes tu posición).

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

    old_style = (
        '      var col = color(idx);\n'
        '      var gj = L.geoJSON(fc, {\n'
        '        style: function () {\n'
        '          return { color: col, weight: 2, fillColor: col, fillOpacity: 0.25 };\n'
        '        },'
    )
    new_style = (
        '      var col = color(idx);\n'
        f'      var CATEGORY_FIELD = {js_str(field)};\n'
        f'      var CATEGORY_COLORS = {color_js};\n'
        '      var gj = L.geoJSON(fc, {\n'
        '        style: function (f) {\n'
        '          var val = f && f.properties ? f.properties[CATEGORY_FIELD] : null;\n'
        '          var c = CATEGORY_COLORS[val] || col;\n'
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
        f'    var CATEGORY_COLORS = {color_js};\n'
        '    Object.keys(CATEGORY_COLORS).forEach(function (label) {\n'
        '      var row = el("div", "dash-legend-row");\n'
        '      row.style.display = "flex";\n'
        '      row.style.alignItems = "center";\n'
        '      row.style.gap = "8px";\n'
        '      row.style.marginBottom = "4px";\n'
        '      var swatch = el("span", "dash-legend-swatch");\n'
        '      swatch.style.display = "inline-block";\n'
        '      swatch.style.width = "14px";\n'
        '      swatch.style.height = "14px";\n'
        '      swatch.style.background = CATEGORY_COLORS[label];\n'
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
        color_map = build_color_map(d, layer_id, field)
        html, applied_colors = patch_category_colors_and_legend(html, field, color_map)
        print(f"  [2/3] Colores por categoría ('{field}', {len(color_map)} valores) + leyenda: "
              f"{', '.join(applied_colors) if applied_colors else 'no aplicable'}")
    else:
        print("  [2/3] No se encontró ningún Chart/Category selector con category_field -> se omite este paso.")

    html, applied_extent = patch_extent_filter(html)
    ok = len(applied_extent) >= 10  # all sub-patches expected
    print(f"  [3/3] Filtro por pan/zoom del mapa: {'aplicado (' + str(len(applied_extent)) + '/11 pasos)' if applied_extent else 'no aplicable'}")
    if applied_extent and not ok:
        print(f"        ⚠️  Solo se aplicaron {len(applied_extent)}/11 pasos — revisa manualmente, "
              "puede que el bundle JS de este export difiera del que usamos como referencia.")

    # insert idempotency marker right after the dashboard-data script closes
    marker_anchor = '</script>\n<script>'
    if marker_anchor in html:
        html = html.replace(marker_anchor, f'</script>\n<script>{MARKER}\n', 1)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"\n✅ Listo. Archivo enriquecido guardado en: {out_path}")


if __name__ == "__main__":
    main()
