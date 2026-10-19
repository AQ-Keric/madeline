import 'package:flutter/material.dart';

import 'tema.dart';
import 'widgets/fondo_montana.dart';

/// La ayuda (el ícono "?" de la cabecera): arriba las secciones y abajo los
/// ejemplos de la elegida, agrupados por lo que se quiere hacer. Tocar una
/// frase la deja escrita en el chat. Las frases son las que entiende el
/// router o Groq; si cambia una forma de pedir algo, actualizarla acá.
class _Grupo {
  final String titulo;
  final List<String> frases;
  const _Grupo(this.titulo, this.frases);
}

class _Seccion {
  final String titulo;
  final IconData icono;
  final String resumen;
  final List<_Grupo> grupos;
  final List<String> bueno;
  const _Seccion(this.titulo, this.icono, this.resumen, this.grupos, this.bueno);
}

const _secciones = [
  _Seccion(
    'Primeros pasos',
    Icons.waving_hand_outlined,
    'Mantén presionado el micrófono mientras hablas y suelta para enviar. También puedes escribirle.',
    [
      _Grupo('Para empezar', ['Hola, Madeline', '¿Qué puedes hacer?', 'Gracias']),
    ],
    [
      'En el PC: di «Madeline, …» y lo que necesitas, o usa Super + J. Super + Shift + J pausa la escucha.',
      'Desde el celular la respuesta suena acá; desde el PC, por los parlantes.',
      'En la pestaña Deck tienes botones para lo de todos los días, sin hablar.',
      'Puedes pedir varias cosas juntas: «pausa la música y baja el volumen».',
    ],
  ),
  _Seccion(
    'Recados',
    Icons.checklist_rounded,
    'Cosas sin hora que no quieres olvidar. Se ven en la barra del PC y en la pestaña Recados.',
    [
      _Grupo('Anotar', ['Recuérdame que debo pagar la luz', 'Tengo que llamar a mi mamá, no me dejes olvidar']),
      _Grupo('Revisar', ['¿Qué recados tengo?', '¿Tengo algo pendiente?']),
      _Grupo('Tachar', ['Ya pagué la luz', 'Borra el recado del pan', 'Borra todos los recados']),
    ],
    [
      '«Recuerda que…» (sin el -me) es para la memoria, no para los recados.',
      'Para tachar basta con decir lo que hiciste: Madeline busca el recado que más se parece.',
    ],
  ),
  _Seccion(
    'Agenda',
    Icons.event_outlined,
    'Tu Google Calendar. La fecha dila como te salga: «el jueves», «el 24», «la otra semana».',
    [
      _Grupo('Consultar', ['¿Qué tengo hoy?', '¿Qué tengo mañana?', '¿Qué tengo esta semana?']),
      _Grupo('Agregar', [
        'Agéndame dentista el jueves a las 4',
        'Tengo reunión mañana a las 10 de la mañana',
        'Agenda el cumple de la Coni el 3 de noviembre',
      ]),
    ],
    [
      'Si no dices «de la mañana» o «de la tarde»: de 1 a 6 es de la tarde, y lo demás, la próxima vez que llegue esa hora. Madeline te confirma la hora.',
      'Si no dices el día, es para hoy.',
      'Si se vence el permiso de Google, Madeline te abre el navegador en el PC para renovarlo.',
    ],
  ),
  _Seccion(
    'Recordatorios',
    Icons.alarm_outlined,
    'Para lo que pasa dentro de un rato: Madeline te lo dice en voz alta en el PC.',
    [
      _Grupo('Avisos', [
        'Recuérdame en 10 minutos sacar la ropa',
        'Avísame en media hora que salga a comprar',
        'Recuérdame en una hora que llame a la Coni',
      ]),
    ],
    ['Para una hora del reloj («a las 5») usa la agenda.'],
  ),
  _Seccion(
    'Música y volumen',
    Icons.music_note_outlined,
    'Controla lo que esté sonando en el PC.',
    [
      _Grupo('Música', ['Pausa', 'Siguiente canción', 'Ponle play', '¿Qué canción es esta?', 'Pon música']),
      _Grupo('Volumen', ['Sube el volumen', 'Baja el volumen un poco', 'Pon el volumen al 40', 'Silencio']),
    ],
    ['«Pon música» sin nada sonando abre tu radio de YouTube Music.'],
  ),
  _Seccion(
    'Apps y ventanas',
    Icons.apps_rounded,
    'Abre, cierra y ordena apps en los escritorios de Hyprland.',
    [
      _Grupo('Apps', ['Abre Steam', 'Abre Discord en el escritorio 2', 'Cierra el navegador', 'Cierra todo']),
      _Grupo('Escritorios', ['Mueve Spotify al escritorio 3', 'Cámbiate al escritorio 2']),
      _Grupo('Rutinas', ['Modo juego', 'Modo trabajo']),
    ],
    [
      'Modo juego cierra Discord y el navegador y abre Steam. Modo trabajo abre el navegador, VS Code y Discord.',
      '«Cierra todo» (modo pánico) cierra el navegador, Minecraft y Bottles.',
    ],
  ),
  _Seccion(
    'Pantalla y web',
    Icons.screenshot_monitor_outlined,
    'Capturas, preguntas sobre lo que hay en pantalla y búsquedas.',
    [
      _Grupo('Pantalla', ['Toma una captura', 'Analiza mi pantalla', 'Analiza mi pantalla y dime qué error es']),
      _Grupo('Buscar', [
        'Busca quién ganó el partido',
        'Búscame papers sobre redes neuronales',
        'Búscame imágenes de gatos',
      ]),
    ],
    ['Las capturas pedidas desde el PC llegan solas a este chat.'],
  ),
  _Seccion(
    'El PC',
    Icons.computer_outlined,
    'Estado, actualizaciones, Bluetooth y energía.',
    [
      _Grupo('Estado', ['¿Cómo está el PC?', '¿Cuánta RAM estoy usando?', '¿Hay actualizaciones?']),
      _Grupo('Bluetooth', ['Conecta los audífonos', 'Desconecta la Alexa', 'Apaga el Bluetooth']),
      _Grupo('Energía', ['Apaga el PC', 'Reinicia el PC']),
    ],
    [
      'Apagar o reiniciar siempre pide que confirmes con un «sí».',
      'El panel del corazón, arriba, muestra CPU, RAM y GPU en tiempo real.',
    ],
  ),
  _Seccion(
    'Clima',
    Icons.wb_sunny_outlined,
    'El clima de Villa Alegre, hasta pasado mañana.',
    [
      _Grupo('Consultar', ['¿Cómo está el clima?', '¿Va a llover mañana?', 'El clima para pasado mañana']),
    ],
    [],
  ),
  _Seccion(
    'Memoria',
    Icons.psychology_outlined,
    'Lo que le cuentas a Madeline lo recuerda en tu PC, aunque pase el tiempo.',
    [
      _Grupo('Guardar', ['Recuerda que mi hermana se llama Ana', 'Estoy aprendiendo Flutter']),
      _Grupo('Preguntar', ['¿Qué recuerdas de mi hermana?', '¿Qué sabes de mí?']),
      _Grupo('Olvidar', ['Olvida lo de mi hermana']),
    ],
    ['Los recuerdos no salen del PC; a la IA solo llegan los 2 o 3 que vienen al caso.'],
  ),
  _Seccion(
    'Preguntas',
    Icons.chat_outlined,
    'Para todo lo demás, Madeline le pregunta a la IA.',
    [
      _Grupo('Lo que sea', [
        '¿Qué puedo cocinar con arroz y huevos?',
        '¿Cuánto es 15 por 23?',
        'Explícame qué es una API',
      ]),
    ],
    [
      'Las respuestas son cortas porque se leen en voz alta. Pide «más detalle» si lo necesitas.',
      'La IA tiene un límite diario. Si se agota, Madeline avisa y las órdenes del PC siguen funcionando.',
    ],
  ),
];

class PantallaAyuda extends StatefulWidget {
  /// Se llama con la frase tocada, para dejarla escrita en el chat.
  final void Function(String frase) alElegirFrase;

  const PantallaAyuda({super.key, required this.alElegirFrase});

  @override
  State<PantallaAyuda> createState() => _PantallaAyudaState();
}

class _PantallaAyudaState extends State<PantallaAyuda> {
  int _elegida = 0;

  Widget _chips() {
    return SizedBox(
      height: 44,
      child: ListView.separated(
        scrollDirection: Axis.horizontal,
        padding: const EdgeInsets.symmetric(horizontal: 16),
        itemCount: _secciones.length,
        separatorBuilder: (_, __) => const SizedBox(width: 8),
        itemBuilder: (_, i) {
          final s = _secciones[i];
          final activa = i == _elegida;
          return ChoiceChip(
            avatar: Icon(s.icono, size: 18, color: activa ? Colors.white : Colores.textoSuave),
            label: Text(s.titulo),
            selected: activa,
            showCheckmark: false,
            selectedColor: Colores.pelo,
            backgroundColor: Colores.superficie,
            side: BorderSide(color: activa ? Colores.pelo : Colores.borde),
            labelStyle: TextStyle(
              color: activa ? Colors.white : Colores.texto,
              fontWeight: activa ? FontWeight.w700 : FontWeight.w500,
            ),
            onSelected: (_) => setState(() => _elegida = i),
          );
        },
      ),
    );
  }

  Widget _frase(String frase) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Material(
        color: Colores.superficie.withValues(alpha: 0.9),
        borderRadius: BorderRadius.circular(14),
        child: InkWell(
          borderRadius: BorderRadius.circular(14),
          onTap: () {
            widget.alElegirFrase(frase);
            Navigator.pop(context);
          },
          child: Container(
            padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 12),
            decoration: BoxDecoration(
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: Colores.borde),
            ),
            child: Row(
              children: [
                const Icon(Icons.format_quote_rounded, size: 18, color: Colores.fresa),
                const SizedBox(width: 10),
                Expanded(child: Text(frase, style: const TextStyle(fontSize: 15))),
                const Icon(Icons.north_east_rounded, size: 16, color: Colores.textoSuave),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _contenido(_Seccion s) {
    return ListView(
      key: ValueKey(s.titulo),
      padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
      children: [
        Text(s.resumen, style: const TextStyle(fontSize: 15.5, height: 1.4)),
        for (final g in s.grupos) ...[
          const SizedBox(height: 20),
          Text(g.titulo.toUpperCase(),
              style: const TextStyle(
                  color: Colores.textoSuave, fontSize: 12, fontWeight: FontWeight.w700, letterSpacing: 1.1)),
          const SizedBox(height: 8),
          for (final f in g.frases) _frase(f),
        ],
        if (s.bueno.isNotEmpty) ...[
          const SizedBox(height: 20),
          Container(
            padding: const EdgeInsets.all(14),
            decoration: BoxDecoration(
              color: Colores.dash.withValues(alpha: 0.10),
              borderRadius: BorderRadius.circular(14),
              border: Border.all(color: Colores.dash.withValues(alpha: 0.35)),
            ),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Row(
                  children: [
                    Icon(Icons.lightbulb_outline_rounded, size: 18, color: Colores.dash),
                    SizedBox(width: 6),
                    Text('Bueno saber', style: TextStyle(color: Colores.dash, fontWeight: FontWeight.w700)),
                  ],
                ),
                for (final b in s.bueno)
                  Padding(
                    padding: const EdgeInsets.only(top: 8),
                    child: Text('• $b', style: const TextStyle(height: 1.4)),
                  ),
              ],
            ),
          ),
        ],
      ],
    );
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: FondoMontana(
        child: SafeArea(
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Padding(
                padding: const EdgeInsets.fromLTRB(4, 8, 16, 4),
                child: Row(
                  children: [
                    IconButton(
                      icon: const Icon(Icons.arrow_back_rounded),
                      onPressed: () => Navigator.pop(context),
                    ),
                    const Expanded(
                      child: Text('Cómo hablarle',
                          style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, letterSpacing: 0.5)),
                    ),
                  ],
                ),
              ),
              const Padding(
                padding: EdgeInsets.fromLTRB(20, 0, 20, 12),
                child: Text('Toca una frase para dejarla escrita en el chat.',
                    style: TextStyle(color: Colores.textoSuave, fontSize: 13)),
              ),
              _chips(),
              Expanded(
                child: AnimatedSwitcher(
                  duration: const Duration(milliseconds: 180),
                  child: _contenido(_secciones[_elegida]),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
