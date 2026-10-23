import 'dart:typed_data';

import 'package:flutter/material.dart';

import '../tema.dart';
import 'avatar_madeline.dart';

class Mensaje {
  String texto;
  final bool deMadeline;
  final bool esError;
  Uint8List? imagen;

  Mensaje(this.texto, {required this.deMadeline, this.esError = false, this.imagen});
}

class Burbuja extends StatelessWidget {
  final Mensaje mensaje;
  final VoidCallback? alTocarImagen;
  const Burbuja({super.key, required this.mensaje, this.alTocarImagen});

  @override
  Widget build(BuildContext context) {
    final deElla = mensaje.deMadeline;
    final color = mensaje.esError
        ? const Color(0xFF3A1A3A)
        : deElla
            ? Colores.superficie
            : Colores.pelo;

    final burbuja = Container(
      constraints: BoxConstraints(maxWidth: MediaQuery.of(context).size.width * 0.75),
      padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.only(
          topLeft: const Radius.circular(18),
          topRight: const Radius.circular(18),
          bottomLeft: Radius.circular(deElla ? 4 : 18),
          bottomRight: Radius.circular(deElla ? 18 : 4),
        ),
        border: deElla ? Border.all(color: mensaje.esError ? Colores.peligro : Colores.borde, width: 1) : null,
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (mensaje.texto.isNotEmpty)
            Text(mensaje.texto, style: const TextStyle(fontSize: 15.5, height: 1.4, color: Colores.texto)),
          if (mensaje.imagen != null) ...[
            if (mensaje.texto.isNotEmpty) const SizedBox(height: 8),
            GestureDetector(
              onTap: alTocarImagen,
              child: ClipRRect(
                borderRadius: BorderRadius.circular(12),
                child: Image.memory(mensaje.imagen!, fit: BoxFit.cover),
              ),
            ),
          ],
        ],
      ),
    );

    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        mainAxisAlignment: deElla ? MainAxisAlignment.start : MainAxisAlignment.end,
        crossAxisAlignment: CrossAxisAlignment.end,
        children: [
          if (deElla) ...[
            const CircleAvatar(radius: 14, backgroundImage: AssetImage('assets/logo.png')),
            const SizedBox(width: 8),
          ],
          Flexible(child: burbuja),
        ],
      ),
    );
  }
}

/// Tres puntitos mientras Madeline piensa.
class Escribiendo extends StatefulWidget {
  const Escribiendo({super.key});

  @override
  State<Escribiendo> createState() => _EscribiendoState();
}

class _EscribiendoState extends State<Escribiendo> with SingleTickerProviderStateMixin {
  late final AnimationController _anim = AnimationController(vsync: this, duration: const Duration(milliseconds: 1100))
    ..repeat();

  @override
  void dispose() {
    _anim.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Padding(
      padding: const EdgeInsets.symmetric(vertical: 4),
      child: Row(
        children: [
          // Ella pensando, mientras llega la respuesta.
          AvatarMadeline.busto(estado: EstadoAvatar.pensando, diametro: 28, fondo: Colores.superficieAlta),
          const SizedBox(width: 8),
          Container(
            padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 14),
            decoration: BoxDecoration(
              color: Colores.superficie,
              borderRadius: BorderRadius.circular(18),
              border: Border.all(color: Colores.borde),
            ),
            child: AnimatedBuilder(
              animation: _anim,
              builder: (_, __) => Row(
                children: List.generate(3, (i) {
                  final fase = ((_anim.value - i * 0.18) % 1.0);
                  final alto = fase < 0.5 ? fase * 2 : (1 - fase) * 2;
                  return Container(
                    margin: const EdgeInsets.symmetric(horizontal: 3),
                    width: 7,
                    height: 7,
                    transform: Matrix4.translationValues(0, -4 * alto, 0),
                    decoration: BoxDecoration(
                      color: Colores.textoSuave.withValues(alpha: 0.5 + alto * 0.5),
                      shape: BoxShape.circle,
                    ),
                  );
                }),
              ),
            ),
          ),
        ],
      ),
    );
  }
}
