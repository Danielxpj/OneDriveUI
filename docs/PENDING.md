# OneDriveUI — Estado y trabajo pendiente

**Última actualización:** 2026-09-01
**Estado:** los 15 paquetes construidos · suite en verde · `onedriveui --state` funciona contra el mundo real

Cliente de escritorio para rclone en Linux que reproduce el cliente de OneDrive de Windows 11.
Python 3.14 + PySide6 6.11.2 (instalación de sistema, sin venv).

---

## Cómo retomar

```bash
cd ~/OneDriveUI
QT_QPA_PLATFORM=offscreen python3 -m pytest -q     # ~3 min
python3 -m onedriveui --doctor                     # qué falta en esta máquina
python3 -m onedriveui --state                      # el estado real, sin interfaz
```

Los tres documentos que mandan sobre el código están en `docs/`:

| Documento | Para qué sirve |
|---|---|
| `docs/BUILD_PLAN.md` | Los 15 paquetes, qué ficheros posee cada uno y sus criterios de aceptación |
| `docs/CONTRACTS.md` | Firmas congeladas. Todo el código nuevo se escribe contra estas |
| `docs/ARCHITECTURE.md` | §3 invariantes · §5 procesos · §6 máquina de estados · §7 hilos · §9 config · §10 DDL |
| `docs/research/*.md` | 8 documentos verificados empíricamente contra esta máquina |

---

## Completado — los 15 paquetes

| Paquete | Contenido |
|---|---|
| **WP-00** contratos | 27 dataclasses, 28 enums, 33 señales, tokens de tema, esquema de 18 tablas, fakes |
| **WP-01** cimientos | escritura atómica, config de 21 secciones, hilo único de escritura, 8 repositorios |
| **WP-02** transporte rc | cliente rc asíncrono, prueba de propiedad del daemon, supervisor del montaje, 7 guardas |
| **WP-03** operaciones rc | `ops` · `auth` · `jobs` · `stats` |
| **WP-04** VFS y bisync | `vfs` · `bisync` · `bisync_log` · `filters` |
| **WP-05** motor | `facts` · `reducer` (escalera de 17 peldaños) · `supervisor` (`do()` como única entrada) |
| **WP-06** pausa y cuentas | `pause` · `bandwidth` · `quota` · `accounts` |
| **WP-07** actividad e incidencias | `activity` · `issues` · `preflight` · `conflicts` · `decisions` |
| **WP-08** Files On-Demand | `pinner` · `filestate` · `browse` · `selective` |
| **WP-09** extras | `versions` · `trashbin` · `sharing` · `kfm` · `watcher` · `extras` · `vault` |
| **WP-10** plataforma | bomba GLib, notificaciones Gio, red/batería, systemd, papelera, IPC |
| **WP-11** kit de widgets | controles Fluent, indicadores, tarjetas, delegado de actividad, árbol tri-estado |
| **WP-12** interfaz | `activity_model` · `activity_center` · `tray` · `notices` · `filebrowser` |
| **WP-13** configuración | `settings_window` · 4 páginas · 4 módulos de diálogos · `wizard` de 7 páginas |
| **WP-14** integración | extensión de Nautilus · `install` · `app.py` · `__main__.py` · `pyproject.toml` · `packaging/` |

Hojas de contacto para revisión visual: `docs/gallery-light.png`, `docs/gallery-dark.png`,
`docs/wp11a-contact-sheet.png`, `docs/wp12a-activity-center.png`.

---

## Hitos

| | Puerta | Estado |
|---|---|---|
| M1 | `onedriveui --state` imprime un `SyncState` correcto sin interfaz | **hecho** — verificado contra un HOME aislado |
| M2 | Bandeja y Centro de actividad con estado real; pausa; sobrevive a `SIGKILL` | código completo; **falta prueba en sesión real** |
| M3 | Fijar, liberar espacio y descargar todo de extremo a extremo | código completo; **falta prueba con la cuenta real** |
| M4 | Configuración, diálogos, OOBE, compartir, KFM, versiones, papelera, vault | código completo; **falta recorrido manual** |
| M5 | Emblemas de Nautilus, columna Estado, menú contextual, avisos, autoarranque | código completo; **falta prueba en Nautilus real** |
| M6 | Cada invariante con test; 24 h de estrés sin pérdida de datos | **pendiente** |

---

## Cómo probarlo a mano

```bash
scripts/livetest.sh --doctor     # cada autocomprobación, contra tu cuenta real
scripts/livetest.sh --status     # una instantánea JSON
scripts/livetest.sh              # la interfaz, en tu pantalla real
scripts/livetest.sh --teardown   # deshacerlo todo
scripts/preview.py --list        # ventanas y diálogos que se pueden abrir sueltos
scripts/preview.py settings      # abrir uno
scripts/preview.py --all --shot /tmp/ui   # un PNG de cada uno, sin pantalla
```

**`scripts/livetest.sh` monta un subdirectorio dedicado, no la unidad entera.** Crea
`onedrive:OneDriveUI-test` (la única escritura que hace en tu cuenta), un remoto `alias`
`onedriveui_test:` que apunta exactamente a esa carpeta, y monta *eso* en `~/.onedriveui-test`. El
cliente no puede ver nada más de tu cuenta: el alias es su universo entero.

**El punto es un directorio oculto a propósito.** Un segundo OneDrive en la barra lateral de Nautilus,
justo debajo del real, es una buena manera de dejar un fichero donde no va. GLib decide qué mostrar
ahí en `g_unix_mount_guess_should_display`, y una de sus reglas oculta cualquier montaje cuyo camino
contenga un componente con punto. Comprobado en glib 2.88.3: montar en `~/zz-hidetest` produjo una
entrada en la barra lateral; montar en `~/.zz-hidetest-dot/mnt` no produjo ninguna. (`-o x-gvfs-hide`
no sirve: `fusermount3` descarta las opciones que no conoce antes de que lleguen al núcleo.) Para
abrirlo: `Ctrl+L` en Nautilus y escribir el camino, o `Ctrl+H` para ver los ocultos.

La razón es concreta. La primera versión montaba `onedrive:` una segunda vez, junto al montaje que ya
tenías. Dos montajes del mismo remoto tienen cada uno su propia caché de directorios y ninguno sabe
del otro; renombrar en uno deja al otro con el listado viejo durante `--dir-cache-time`. Un renombrado
hecho contra ese listado viejo hace lo que rclone siempre hace al sobreescribir: borra el destino y
luego mueve el origen. El borrado llega al servidor; el movimiento falla con `itemNotFound`. Así se
perdió `LEY CANNABIS obsoleta.docx` el 2026-09-01. El subdirectorio no elimina el peligro —es
inherente a dos montajes de una cuenta— lo encierra en una carpeta que existe para pruebas. El script
además baja `dir_cache_time_s` de una hora a un minuto, para que la ventana de desacuerdo sea corta.

`--teardown` para las unidades, desmonta, borra el remoto alias y el estado local, y **deja la carpeta
en la nube a propósito**: borrar una carpeta de tu cuenta real no es algo que el script haga por ti
(`rclone purge onedrive:OneDriveUI-test`). Tambien desmonta y limpia el montaje visible
de la version anterior, `~/OneDriveUI-test`, y borra su cuenta de la configuracion.

Lo que el script **no** hace: ejecutar el wizard (escribiría `RCLONE_TEST` en la raíz), instalar la
extensión de Nautilus, instalar iconos ni configurar el autoarranque. Todo eso son órdenes aparte.

---

## Lo que queda

Todo lo pendiente es **verificación contra el mundo real**, no construcción. El código de los 15
paquetes está escrito y probado en aislamiento; nada se ha ejercitado todavía contra la cuenta
real del usuario ni dentro de una sesión gráfica de verdad.

1. **Un recorrido en la sesión gráfica real.** `python3 -m onedriveui` con la cuenta configurada:
   que aparezca el icono en la bandeja (hay `appindicatorsupport` instalado y `gnome-shell` posee
   `org.kde.StatusNotifierWatcher`, así que debería), que el menú se despliegue, que el Centro de
   actividad abra y que el estado sea el correcto.
2. **La extensión en un Nautilus real.** `onedriveui --install-extension`, luego `nautilus -q`, y
   comprobar los emblemas con `NAUTILUS_PYTHON_DEBUG=misc`. El tema del usuario es breeze-dark, que
   no conoce `emblem-onedriveui-*`, así que este es justo el caso que depende del respaldo hicolor.
3. **Un `rclone rcd` propio en el rango 17800–17899** para ejercitar el transporte de verdad. Los
   puertos 5572/5573/53682 siguen prohibidos.
4. **Las 24 h de estrés de M6.**

---

## Decisión ya tomada

La pregunta abierta del 2026-08-31 (¿seguir con workflows multi-agente, construir directamente, o
hacer primero un mínimo ejecutable?) se resolvió como **construcción directa sin subagentes**:
ultracode seguía desactivado y no hubo autorización explícita para lanzar más workflows. Las
oleadas 3 y 4 se escribieron secuencialmente.

---

## Cosas que no hay que olvidar

- **El montaje actual del usuario lleva `--onedrive-chunk-size 30M` en la línea de órdenes**, lo que
  renombra el sistema de ficheros a `onedrive{MxOuf}:` y deja huérfana la caché VFS. Por eso hay dos
  árboles abandonados en `~/.cache/rclone/vfs/`. El invariante I1 lo prohíbe; el panel "Acerca de"
  ofrece recuperarlos y `Supervisor.reclaim_orphaned_cache()` los borra con las dos guardas puestas.
- **Puertos 5572, 5573 y 53682 prohibidos.** Para pruebas, levantar un `rclone rcd` propio en el
  rango 17800–17899 y matarlo al terminar.
- **Nunca mutar el remoto real `onedrive:`.** Solo lecturas: `about`, `lsjson`, `backend features`.
- **Hay bandeja en esta máquina.** `gnome-extensions list` muestra `appindicatorsupport@rgcjonas.gmail.com`
  y `busctl --user list` muestra `org.kde.StatusNotifierWatcher` en poder de gnome-shell. No hace
  falta el repliegue documentado, pero `ui/tray.available()` sigue comprobándolo — y devuelve `False`
  sin `QApplication`, porque `isSystemTrayAvailable()` **segfaultea** si se llama antes.
- **`strings.py` ganó una clave nueva**, `DIALOG.REMOVE_LINK` ("Remove link"). La tabla congelada no
  nombraba el control que WP-13 tiene que mostrar deshabilitado, y un literal en el diálogo habría
  incumplido la regla de que ninguna cadena visible vive fuera de `strings.py`.
- **Los enlaces web caducan y nadie avisa.** `WEB_RECYCLE_BIN` pasó de
  `?id=recyclebin` a `/recycle` (corregido el 2026-09-01, reportado desde un clic real).
  Los otros tres de `constants.py` — `WEB_ROOT`, `WEB_GET_MORE_STORAGE`, `WEB_RESTORE` — son
  de la misma cosecha y **no están verificados**. El de la papelera es el que más importa:
  rclone no puede listar la papelera de OneDrive, así que ese enlace es la única ruta del
  usuario a lo que borró desde el gestor de archivos.
- **`WEB_RESTORE` es una constante muerta.** Su sitio natural es el botón primario del
  diálogo de borrado masivo ("Restore files"), que hoy cierra el diálogo y no lleva a
  ninguna parte. Decisión pendiente: cablearlo (restaurar primero desde nuestra papelera,
  la web como salida) o borrar la constante.
- Los 15 invariantes duros están en `ARCHITECTURE.md` §3 y se aplican en `rc/guards.py`.
