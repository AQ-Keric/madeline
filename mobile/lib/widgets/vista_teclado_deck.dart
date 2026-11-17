import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import '../conexion.dart';
import '../tema.dart';

Color _parseHex(String hex) {
  final limpia = hex.replaceAll('#', '').trim();
  if (limpia.length == 6) {
    return Color(int.tryParse('FF$limpia', radix: 16) ?? 0xFF8E52E0);
  }
  return const Color(0xFF8E52E0);
}

class VistaTecladoDeck extends StatefulWidget {
  const VistaTecladoDeck({
    super.key,
    required this.conexion,
    required this.alVolver,
  });

  final Conexion conexion;
  final VoidCallback alVolver;

  @override
  State<VistaTecladoDeck> createState() => _VistaTecladoDeckState();
}

class _VistaTecladoDeckState extends State<VistaTecladoDeck> {
  bool _cargando = true;
  String? _error;

  List<Map<String, dynamic>> _modelos = [];
  String _modeloActivo = 'kai_65';
  String _temaActivo = 'wow_souso_frost';
  int _brillo = 100;
  List<String> _favoritos = [];
  List<Map<String, dynamic>> _temas = [];

  String _filtroTexto = '';
  String _categoria = 'todos';
  bool _enviando = false;

  @override
  void initState() {
    super.initState();
    _cargarDatos();
  }

  Future<void> _cargarDatos() async {
    setState(() {
      _cargando = true;
      _error = null;
    });

    try {
      if (widget.conexion.ip == null || widget.conexion.estado != EstadoConexion.conectada) {
        await widget.conexion.conectar();
      }

      var estado = await widget.conexion.deckTecladoEstado();
      var catalogo = await widget.conexion.deckTecladoTemas();

      if (estado == null) {
        // Reintentar conectando de nuevo al PC
        await widget.conexion.conectar(forzarBusqueda: true);
        estado = await widget.conexion.deckTecladoEstado();
        catalogo = await widget.conexion.deckTecladoTemas();
      }

      if (!mounted) return;

      if (estado == null) {
        setState(() {
          _error = 'No pude conectar con el PC para leer el teclado.\nVerifica que el PC esté en la misma red Wi-Fi.';
          _cargando = false;
        });
        return;
      }

      final datos = estado;
      setState(() {
        _modelos = (datos['modelos'] as List?)?.cast<Map<String, dynamic>>() ?? [];
        _modeloActivo = datos['modelo_activo']?.toString() ?? 'kai_65';
        _temaActivo = datos['tema_activo']?.toString() ?? 'wow_souso_frost';
        _brillo = (datos['brillo'] as num?)?.toInt() ?? 100;
        _favoritos = (datos['favoritos'] as List?)?.map((e) => e.toString()).toList() ?? [];
        _temas = catalogo ?? [];
        _cargando = false;
      });
    } catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Error de conexión: $e';
          _cargando = false;
        });
      }
    }
  }

  Future<void> _aplicarTema(String temaId) async {
    if (_enviando) return;
    HapticFeedback.selectionClick();
    setState(() {
      _temaActivo = temaId;
      _enviando = true;
    });

    final ok = await widget.conexion.deckTecladoAplicar(
      temaId,
      brillo: _brillo,
      modelo: _modeloActivo,
    );

    if (!mounted) return;
    setState(() => _enviando = false);

    if (!ok) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('No se pudo aplicar el diseño al teclado.')),
      );
    }
  }

  Future<void> _cambiarBrillo(int nuevoBrillo) async {
    HapticFeedback.lightImpact();
    setState(() => _brillo = nuevoBrillo);
    await widget.conexion.deckTecladoAplicar(
      _temaActivo,
      brillo: nuevoBrillo,
      modelo: _modeloActivo,
    );
  }

  Future<void> _cambiarModelo(String idModelo) async {
    HapticFeedback.mediumImpact();
    setState(() => _modeloActivo = idModelo);
    await widget.conexion.deckTecladoCambiarModelo(idModelo);
  }

  Future<void> _toggleFavorito(String temaId) async {
    HapticFeedback.lightImpact();
    setState(() {
      if (_favoritos.contains(temaId)) {
        _favoritos.remove(temaId);
      } else {
        _favoritos.add(temaId);
      }
    });
    await widget.conexion.deckTecladoGuardarFavoritos(_favoritos);
  }

  void _mostrarSelectorModelos() {
    showModalBottomSheet(
      context: context,
      backgroundColor: Colores.superficie,
      builder: (ctx) => Padding(
        padding: const EdgeInsets.fromLTRB(20, 8, 20, 28),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Seleccionar Teclado o Perfil',
              style: TextStyle(fontSize: 18, fontWeight: FontWeight.bold, color: Colores.texto),
            ),
            const SizedBox(height: 6),
            const Text(
              'Elige el teclado físico o modo de juego a controlar:',
              style: TextStyle(color: Colores.textoSuave, fontSize: 13),
            ),
            const SizedBox(height: 16),
            for (final m in _modelos) ...[
              ListTile(
                contentPadding: const EdgeInsets.symmetric(horizontal: 12, vertical: 4),
                shape: RoundedRectangleBorder(
                  borderRadius: BorderRadius.circular(14),
                  side: BorderSide(
                    color: m['id'] == _modeloActivo ? Colores.dash : Colores.borde,
                    width: m['id'] == _modeloActivo ? 2 : 1,
                  ),
                ),
                tileColor: m['id'] == _modeloActivo
                    ? Colores.dash.withValues(alpha: 0.15)
                    : Colores.superficieAlta,
                leading: Icon(
                  m['id'] == 'kai_65'
                      ? Icons.keyboard_rounded
                      : m['id'] == 'perfil_mmo'
                          ? Icons.sports_esports_rounded
                          : Icons.nightlight_round,
                  color: m['id'] == _modeloActivo ? Colores.dash : Colores.textoSuave,
                  size: 28,
                ),
                title: Row(
                  children: [
                    Text(
                      m['nombre']?.toString() ?? '',
                      style: TextStyle(
                        fontWeight: FontWeight.bold,
                        color: m['id'] == _modeloActivo ? Colors.white : Colores.texto,
                      ),
                    ),
                    const SizedBox(width: 8),
                    if (m['conectado'] == true && m['id'] == 'kai_65')
                      Container(
                        padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 2),
                        decoration: BoxDecoration(
                          color: Colores.ok.withValues(alpha: 0.2),
                          borderRadius: BorderRadius.circular(6),
                        ),
                        child: const Text('Conectado', style: TextStyle(color: Colores.ok, fontSize: 11)),
                      ),
                  ],
                ),
                subtitle: Text(
                  m['descripcion']?.toString() ?? '',
                  style: const TextStyle(color: Colores.textoSuave, fontSize: 12),
                ),
                trailing: m['id'] == _modeloActivo
                    ? const Icon(Icons.check_circle_rounded, color: Colores.dash)
                    : null,
                onTap: () {
                  Navigator.pop(ctx);
                  _cambiarModelo(m['id'].toString());
                },
              ),
              const SizedBox(height: 8),
            ],
          ],
        ),
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    if (_cargando) {
      return const Center(child: CircularProgressIndicator(color: Colores.dash));
    }

    if (_error != null) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24.0),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.keyboard_rounded, size: 48, color: Colores.peligro),
              const SizedBox(height: 12),
              Text(_error!, textAlign: TextAlign.center, style: const TextStyle(color: Colores.textoSuave)),
              const SizedBox(height: 16),
              OutlinedButton.icon(
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Reintentar'),
                onPressed: _cargarDatos,
              ),
            ],
          ),
        ),
      );
    }

    // Filtrar catálogo
    final filtro = _filtroTexto.toLowerCase().trim();
    final temasFiltrados = _temas.where((t) {
      final nombre = (t['nombre'] ?? '').toString().toLowerCase();
      final cat = (t['categoria'] ?? '').toString();
      final desc = (t['descripcion'] ?? '').toString().toLowerCase();
      final coincideFiltro = filtro.isEmpty || nombre.contains(filtro) || desc.contains(filtro);
      final coincideCat = _categoria == 'todos' || cat == _categoria;
      return coincideFiltro && coincideCat;
    }).toList();

    // Modelo actual
    final modeloObj = _modelos.firstWhere(
      (m) => m['id'] == _modeloActivo,
      orElse: () => {'nombre': 'Pegasum KAI 65%', 'conectado': true},
    );

    return Column(
      children: [
        // Cabecera con botón volver y selector de modelo
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 4, 16, 8),
          child: Row(
            children: [
              IconButton(
                icon: const Icon(Icons.arrow_back_rounded, color: Colores.texto),
                tooltip: 'Volver al Deck',
                onPressed: widget.alVolver,
              ),
              const SizedBox(width: 4),
              Expanded(
                child: InkWell(
                  onTap: _mostrarSelectorModelos,
                  borderRadius: BorderRadius.circular(12),
                  child: Container(
                    padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
                    decoration: BoxDecoration(
                      color: Colores.superficieAlta,
                      borderRadius: BorderRadius.circular(12),
                      border: Border.all(color: Colores.borde),
                    ),
                    child: Row(
                      children: [
                        const Icon(Icons.keyboard_rounded, color: Colores.dash, size: 20),
                        const SizedBox(width: 8),
                        Expanded(
                          child: Column(
                            crossAxisAlignment: CrossAxisAlignment.start,
                            mainAxisSize: MainAxisSize.min,
                            children: [
                              Text(
                                modeloObj['nombre']?.toString() ?? 'Teclado KAI',
                                style: const TextStyle(fontWeight: FontWeight.bold, fontSize: 13),
                                maxLines: 1,
                                overflow: TextOverflow.ellipsis,
                              ),
                              const Text(
                                'Toca para cambiar tipo/perfil',
                                style: TextStyle(color: Colores.textoSuave, fontSize: 10.5),
                              ),
                            ],
                          ),
                        ),
                        const Icon(Icons.keyboard_arrow_down_rounded, color: Colores.textoSuave),
                      ],
                    ),
                  ),
                ),
              ),
              const SizedBox(width: 8),
              // Botón de encender/apagar rápido
              IconButton(
                icon: Icon(
                  _brillo > 0 ? Icons.lightbulb_rounded : Icons.lightbulb_outline_rounded,
                  color: _brillo > 0 ? Colores.aviso : Colores.textoSuave,
                ),
                tooltip: _brillo > 0 ? 'Apagar luces' : 'Encender luces',
                onPressed: () => _cambiarBrillo(_brillo > 0 ? 0 : 100),
              ),
            ],
          ),
        ),

        // SECCIÓN SUPERIOR: SELECTOR DE FAVORITOS (PINNED)
        _seccionFavoritos(),

        // CONTROLES DE BRILLO RÁPIDOS
        _barraBrillo(),

        // BUSCADOR Y FILTROS DEL CATÁLOGO
        Padding(
          padding: const EdgeInsets.fromLTRB(16, 6, 16, 6),
          child: TextField(
            onChanged: (v) => setState(() => _filtroTexto = v),
            style: const TextStyle(color: Colores.texto, fontSize: 13),
            decoration: InputDecoration(
              hintText: 'Buscar entre los 46 temas…',
              hintStyle: const TextStyle(color: Colores.textoSuave, fontSize: 13),
              prefixIcon: const Icon(Icons.search, color: Colores.textoSuave, size: 20),
              isDense: true,
              contentPadding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
              filled: true,
              fillColor: Colores.superficieAlta,
              border: OutlineInputBorder(
                borderRadius: BorderRadius.circular(14),
                borderSide: const BorderSide(color: Colores.borde),
              ),
              enabledBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(14),
                borderSide: const BorderSide(color: Colores.borde),
              ),
              focusedBorder: OutlineInputBorder(
                borderRadius: BorderRadius.circular(14),
                borderSide: const BorderSide(color: Colores.dash),
              ),
            ),
          ),
        ),

        // FILTRO DE CATEGORÍAS
        _pillsCategorias(),

        // GRILLA DE TODOS LOS TEMAS
        Expanded(
          child: temasFiltrados.isEmpty
              ? const Center(
                  child: Text('No hay temas con ese nombre.', style: TextStyle(color: Colores.textoSuave)),
                )
              : GridView.builder(
                  padding: const EdgeInsets.fromLTRB(16, 6, 16, 20),
                  gridDelegate: const SliverGridDelegateWithFixedCrossAxisCount(
                    crossAxisCount: 2,
                    mainAxisSpacing: 10,
                    crossAxisSpacing: 10,
                    childAspectRatio: 1.5,
                  ),
                  itemCount: temasFiltrados.length,
                  itemBuilder: (_, i) {
                    final t = temasFiltrados[i];
                    final tid = t['id'].toString();
                    final esActivo = tid == _temaActivo;
                    final esFav = _favoritos.contains(tid);
                    final colores = (t['preview_colores'] as List?)?.map((c) => c.toString()).toList() ?? [];

                    return _tarjetaTema(
                      temaId: tid,
                      nombre: t['nombre']?.toString() ?? '',
                      icono: t['icono']?.toString() ?? '✨',
                      colores: colores,
                      esActivo: esActivo,
                      esFavorito: esFav,
                    );
                  },
                ),
        ),
      ],
    );
  }

  Widget _seccionFavoritos() {
    final temasMap = {for (final t in _temas) t['id'].toString(): t};
    final listaFavs = _favoritos.where((f) => temasMap.containsKey(f)).toList();

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisSize: MainAxisSize.min,
      children: [
        Padding(
          padding: const EdgeInsets.fromLTRB(20, 4, 20, 6),
          child: Row(
            children: [
              const Icon(Icons.star_rounded, color: Colores.aviso, size: 18),
              const SizedBox(width: 6),
              const Text(
                'FAVORITOS',
                style: TextStyle(
                  color: Colores.aviso,
                  fontWeight: FontWeight.w800,
                  fontSize: 12,
                  letterSpacing: 1.1,
                ),
              ),
              const Spacer(),
              Text(
                '${listaFavs.length} fijados arriba',
                style: const TextStyle(color: Colores.textoSuave, fontSize: 11),
              ),
            ],
          ),
        ),
        SizedBox(
          height: 64,
          child: ListView.separated(
            padding: const EdgeInsets.symmetric(horizontal: 16),
            scrollDirection: Axis.horizontal,
            itemCount: listaFavs.length,
            separatorBuilder: (_, __) => const SizedBox(width: 10),
            itemBuilder: (_, i) {
              final tid = listaFavs[i];
              final t = temasMap[tid]!;
              final esActivo = tid == _temaActivo;
              final icono = t['icono']?.toString() ?? '✨';
              final nombre = t['nombre']?.toString() ?? '';
              final colores = (t['preview_colores'] as List?)?.map((c) => c.toString()).toList() ?? [];

              return GestureDetector(
                onTap: () => _aplicarTema(tid),
                child: AnimatedContainer(
                  duration: const Duration(milliseconds: 160),
                  padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
                  decoration: BoxDecoration(
                    color: esActivo ? Colores.dash.withValues(alpha: 0.22) : Colores.superficieAlta,
                    borderRadius: BorderRadius.circular(16),
                    border: Border.all(
                      color: esActivo ? Colores.dash : Colores.borde,
                      width: esActivo ? 2 : 1,
                    ),
                    boxShadow: [
                      if (esActivo)
                        BoxShadow(
                          color: Colores.dash.withValues(alpha: 0.35),
                          blurRadius: 10,
                          spreadRadius: 1,
                        ),
                    ],
                  ),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: [
                      Text(icono, style: const TextStyle(fontSize: 22)),
                      const SizedBox(width: 8),
                      Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        mainAxisAlignment: MainAxisAlignment.center,
                        children: [
                          Text(
                            nombre,
                            style: TextStyle(
                              fontWeight: esActivo ? FontWeight.bold : FontWeight.w600,
                              fontSize: 12,
                              color: esActivo ? Colors.white : Colores.texto,
                            ),
                          ),
                          const SizedBox(height: 4),
                          // Muestras de colores reales del tema
                          Row(
                            children: [
                              for (final c in colores.take(3))
                                Container(
                                  width: 9,
                                  height: 9,
                                  margin: const EdgeInsets.only(right: 3),
                                  decoration: BoxDecoration(
                                    color: _parseHex(c),
                                    shape: BoxShape.circle,
                                    border: Border.all(color: Colors.white24, width: 0.5),
                                  ),
                                ),
                            ],
                          ),
                        ],
                      ),
                    ],
                  ),
                ),
              );
            },
          ),
        ),
      ],
    );
  }

  Widget _barraBrillo() {
    return Padding(
      padding: const EdgeInsets.fromLTRB(16, 8, 16, 4),
      child: Row(
        children: [
          const Text('Brillo:', style: TextStyle(color: Colores.textoSuave, fontSize: 12)),
          const SizedBox(width: 8),
          for (final b in [0, 25, 50, 75, 100])
            Expanded(
              child: Padding(
                padding: const EdgeInsets.symmetric(horizontal: 2),
                child: InkWell(
                  onTap: () => _cambiarBrillo(b),
                  borderRadius: BorderRadius.circular(8),
                  child: Container(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    decoration: BoxDecoration(
                      color: _brillo == b ? Colores.pelo : Colores.superficieAlta,
                      borderRadius: BorderRadius.circular(8),
                      border: Border.all(
                        color: _brillo == b ? Colores.pelo : Colores.borde,
                      ),
                    ),
                    alignment: Alignment.center,
                    child: Text(
                      b == 0 ? 'OFF' : '$b%',
                      style: TextStyle(
                        fontSize: 11,
                        fontWeight: _brillo == b ? FontWeight.bold : FontWeight.normal,
                        color: _brillo == b ? Colors.white : Colores.textoSuave,
                      ),
                    ),
                  ),
                ),
              ),
            ),
        ],
      ),
    );
  }

  Widget _pillsCategorias() {
    const cats = [
      ('todos', 'Todos (46)'),
      ('2_colors', '2 Colores'),
      ('3_colors', '3 Colores'),
      ('4_colors', '4 Colores'),
      ('multicolor', 'Multicolor'),
    ];

    return SizedBox(
      height: 34,
      child: ListView.separated(
        padding: const EdgeInsets.symmetric(horizontal: 16),
        scrollDirection: Axis.horizontal,
        itemCount: cats.length,
        separatorBuilder: (_, __) => const SizedBox(width: 6),
        itemBuilder: (_, i) {
          final (id, etiqueta) = cats[i];
          final activa = _categoria == id;
          return ChoiceChip(
            label: Text(etiqueta),
            selected: activa,
            onSelected: (_) => setState(() => _categoria = id),
            selectedColor: Colores.dash.withValues(alpha: 0.3),
            backgroundColor: Colores.superficieAlta,
            side: BorderSide(color: activa ? Colores.dash : Colores.borde),
            labelStyle: TextStyle(
              fontSize: 11.5,
              fontWeight: activa ? FontWeight.bold : FontWeight.normal,
              color: activa ? Colors.white : Colores.textoSuave,
            ),
            padding: const EdgeInsets.symmetric(horizontal: 6, vertical: 0),
            visualDensity: VisualDensity.compact,
          );
        },
      ),
    );
  }

  Widget _tarjetaTema({
    required String temaId,
    required String nombre,
    required String icono,
    required List<String> colores,
    required bool esActivo,
    required bool esFavorito,
  }) {
    return GestureDetector(
      onTap: () => _aplicarTema(temaId),
      child: AnimatedContainer(
        duration: const Duration(milliseconds: 140),
        padding: const EdgeInsets.fromLTRB(10, 8, 10, 8),
        decoration: BoxDecoration(
          color: esActivo ? Colores.dash.withValues(alpha: 0.22) : Colores.superficieAlta,
          borderRadius: BorderRadius.circular(16),
          border: Border.all(
            color: esActivo ? Colores.dash : Colores.borde,
            width: esActivo ? 2 : 1,
          ),
          boxShadow: [
            if (esActivo)
              BoxShadow(
                color: Colores.dash.withValues(alpha: 0.3),
                blurRadius: 10,
              ),
          ],
        ),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          mainAxisAlignment: MainAxisAlignment.spaceBetween,
          children: [
            Row(
              children: [
                Text(icono, style: const TextStyle(fontSize: 20)),
                const Spacer(),
                // Botón estrella favorito
                GestureDetector(
                  onTap: () => _toggleFavorito(temaId),
                  child: Padding(
                    padding: const EdgeInsets.all(2.0),
                    child: Icon(
                      esFavorito ? Icons.star_rounded : Icons.star_outline_rounded,
                      color: esFavorito ? Colores.aviso : Colores.textoSuave.withValues(alpha: 0.6),
                      size: 20,
                    ),
                  ),
                ),
              ],
            ),
            Text(
              nombre,
              maxLines: 1,
              overflow: TextOverflow.ellipsis,
              style: TextStyle(
                fontWeight: esActivo ? FontWeight.bold : FontWeight.w600,
                fontSize: 12,
                color: esActivo ? Colors.white : Colores.texto,
              ),
            ),
            // Paleta de colores vivos
            Row(
              children: [
                for (final c in colores.take(4))
                  Container(
                    width: 12,
                    height: 12,
                    margin: const EdgeInsets.only(right: 4),
                    decoration: BoxDecoration(
                      color: _parseHex(c),
                      shape: BoxShape.circle,
                      border: Border.all(color: Colors.white24, width: 0.8),
                    ),
                  ),
                const Spacer(),
                if (esActivo)
                  const Text('ACTIVO', style: TextStyle(color: Colores.dash, fontWeight: FontWeight.w800, fontSize: 9.5)),
              ],
            ),
          ],
        ),
      ),
    );
  }
}
