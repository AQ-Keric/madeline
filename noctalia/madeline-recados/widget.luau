--!nonstrict
-- Recados de Madeline: ícono con la cantidad y los recados en el tooltip.
--
-- Lee data/recados.json, el mismo archivo que escribe Madeline
-- (server/recados.py), así que un recado dicho por voz aparece solo.
-- El clic abre el panel (panel.luau): lo declara plugin.toml en
-- [widget.actions], así que aquí no hay onClick.

local ARCHIVO = noctalia.expandPath("~/asistente_local/data/recados.json")

local recados = {}
local crudo = nil
local leyendo = false

local function render()
    barWidget.setGlyph(noctalia.getConfig("glyph"))
    barWidget.setGlyphColor(#recados > 0 and "primary" or "on_surface")
    barWidget.setText(#recados > 0 and tostring(#recados) or "")

    local filas = {}
    if #recados == 0 then
        table.insert(filas, { key = "", value = "Sin recados" })
    else
        for _, recado in ipairs(recados) do
            table.insert(filas, { key = "•", value = tostring(recado.texto or "") })
        end
    end
    table.insert(filas, { key = "", value = "Clic: abrir los recados" })
    barWidget.setTooltip(filas)
end

local function releer()
    if leyendo then
        return
    end
    leyendo = true
    local ok = noctalia.readFileAsync(ARCHIVO, function(data, _err)
        leyendo = false
        -- Sin archivo es lo normal antes del primer recado.
        data = data or "[]"
        if data == crudo then
            return
        end
        crudo = data
        local lista = noctalia.json.decode(data)
        recados = type(lista) == "table" and lista or {}
        render()
    end)
    if not ok then
        leyendo = false
    end
end

function update()
    releer()
end

noctalia.setUpdateInterval(2000)
releer()
render()
