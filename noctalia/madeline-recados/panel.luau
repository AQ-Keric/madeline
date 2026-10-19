--!nonstrict
-- Panel de los recados: escribir uno nuevo arriba y tacharlos con el cuadrito.
--
-- Escribe el mismo data/recados.json que Madeline (server/recados.py), con
-- el mismo formato: [{ id, texto, creado }].

local ARCHIVO = noctalia.expandPath("~/asistente_local/data/recados.json")
local ANCHO_TEXTO = 300

local recados = {}
local nuevos = 0 -- cambia la key del input para dejarlo vacío tras agregar

local function cargar()
    local crudo = noctalia.readFile(ARCHIVO)
    local lista = crudo ~= nil and noctalia.json.decode(crudo) or nil
    recados = type(lista) == "table" and lista or {}
end

local function guardar()
    local codificado = noctalia.json.encode(recados, true)
    if codificado == nil or not noctalia.writeFile(ARCHIVO, codificado) then
        noctalia.notifyError("Recados", "No pude guardar los recados.")
    end
end

local render

local function agregar(texto)
    texto = noctalia.string.trim(texto or "")
    if texto == "" then
        return
    end
    cargar()
    local maximo = 0
    for _, recado in ipairs(recados) do
        maximo = math.max(maximo, tonumber(recado.id) or 0)
    end
    table.insert(recados, {
        id = maximo + 1,
        texto = string.upper(string.sub(texto, 1, 1)) .. string.sub(texto, 2),
        creado = os.date("%Y-%m-%dT%H:%M"),
    })
    guardar()
    nuevos += 1
    render()
end

local function tachar(id)
    cargar()
    local restantes = {}
    for _, recado in ipairs(recados) do
        if recado.id ~= id then
            table.insert(restantes, recado)
        end
    end
    recados = restantes
    guardar()
    render()
end

local function fila(recado)
    return ui.row({ key = "recado-" .. tostring(recado.id), gap = 6, align = "center" }, {
        ui.button({
            glyph = "square",
            variant = "ghost",
            tooltip = "Tachar",
            onClick = function()
                tachar(recado.id)
            end,
        }),
        ui.label({
            text = tostring(recado.texto or ""),
            width = ANCHO_TEXTO,
            color = "on_surface",
        }),
    })
end

render = function()
    local titulo = #recados > 0 and ("Recados · " .. #recados) or "Recados"
    local cabecera = ui.row({ align = "center", gap = 8 }, {
        ui.glyph({ name = "checklist", size = 18, color = "primary" }),
        ui.label({ text = titulo, fontSize = 16, fontWeight = "bold", color = "on_surface" }),
        ui.spacer({ flexGrow = 1 }),
        ui.button({ glyph = "close", variant = "ghost", tooltip = "Cerrar", onClick = "onCerrar" }),
    })

    local entrada = ui.row({ gap = 8, align = "center" }, {
        ui.input({
            key = "nuevo-" .. nuevos,
            placeholder = "Nuevo recado…",
            focus = true,
            flexGrow = 1,
            onSubmit = agregar,
        }),
    })

    local cuerpo
    if #recados == 0 then
        cuerpo = ui.column({ flexGrow = 1, align = "center", justify = "center" }, {
            ui.label({ text = "Sin recados. Qué orden.", color = "on_surface_variant" }),
        })
    else
        local filas = {}
        for _, recado in ipairs(recados) do
            table.insert(filas, fila(recado))
        end
        cuerpo = ui.scroll({ flexGrow = 1, gap = 4 }, filas)
    end

    panel.render(ui.column({ flexGrow = 1, gap = 12, align = "stretch" }, { cabecera, entrada, cuerpo }))
end

function onOpen(_context)
    cargar()
    panel.setWantsSecondTicks(true)
    render()
end

-- Madeline puede agregar o tachar mientras el panel está abierto.
function update()
    local antes = noctalia.json.encode(recados)
    cargar()
    if noctalia.json.encode(recados) ~= antes then
        render()
    end
end

function onCerrar()
    panel.close()
end
