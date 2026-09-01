# OneDriveUI — Estado y trabajo pendiente

**Última actualización:** 2026-09-01 (tarde)

> ## Lo que cambió esta tarde
>
> Una auditoría de los 15 subsistemas encontró una causa común detrás de casi
> todo: **la raíz de composición no cableaba el motor**. `build_engine()`
> construía diez servicios y dejaba sin construir otros veinticinco, y pasaba
> `vfs_stats=lambda: None` con `stats`, `pin_jobs`, `auth` y `bisync` sin pasar
> siquiera. De ahí salían solos unos treinta defectos: la escalera de 17 peldaños
> decidía con la mitad de los hechos en cero, así que **`SYNCING` era
> inalcanzable por construcción** y el Centro de actividad no podía mostrar ni
> una transferencia.
>
> **Cableado ahora:** `StatsPoller` y el nuevo `VfsStatsPoller` contra el rc del
> *montaje* (que es el único proceso que ve las subidas), `pin_jobs`,
> `BandwidthController` (los reguladores de ancho de banda escribían config que
> nadie leía: `core/bwlimit` no se llamaba nunca), el servidor IPC de Nautilus
> —construido y jamás arrancado— y el guardián de instancia única, que tampoco
> se instalaba nunca: cada clic en el lanzador abría un cliente completo más.
>
> **`IOPool` existe** (`platform/iopool.py`). Diez módulos documentan sus
> llamadas bloqueantes como "IOPool only" según §7.3 y el pool no existía, así
> que corrían en el hilo de la GUI — incluido `quota.refresh()`, que es un viaje
> a Microsoft cada cinco minutos.
>
> **Página nueva: "rclone engine"** (`ui/pages/page_rclone.py`). De los 150
> ajustes de `config.json` la interfaz llegaba a 18, y a **ninguno** de los 28
> parámetros del montaje: `RESTART_REQUIRED_KEYS` ya nombraba doce de ellos sin
> que nada pudiera cambiar uno. La página los expone, marca los que exigen
> remontaje, ofrece el reinicio, y muestra **la línea de órdenes exacta** que
> produce, generada por `build_argv()` — la misma función que escribe la unidad.
>
> **Borrado:** `sync/vault.py` (gocryptfs, no es rclone; la respuesta nativa
> sería un remoto `crypt`), `sync/kfm.py` (mover el Escritorio real del usuario
> a través de un montaje FUSE) y `sync/extras.py` (capturas y cámara, funciones
> de Windows). Ninguno se construía. Con ellos se van dos rutas de pérdida de
> datos que la auditoría había marcado.
>
> **Corregido además:** el enganche `FOREIGN` (un reinicio normal del rcd hacía
> que nuestro propio demonio fallara la prueba de propiedad y el cliente se
> quedaba en ERROR rojo para siempre, incluso tras reiniciar); los dos sondeos
> se quedaban colgados para siempre tras un `abort()`, que no emite señal;
> `do()` se llamaba a sí mismo sin fin en cuatro acciones; `--state` ejecutaba
> los *efectos* de la escalera y podía desmontar un montaje vivo; las claves con
> ámbito de cuenta se resolvían siempre contra la primera cuenta; y `validate()`
> ahora **rechaza dos cuentas sobre el mismo remoto o el mismo punto de
> montaje** — la configuración exacta que borró un fichero real esta mañana.
>
> ### Segunda pasada
>
> La auditoría completa devolvió **111 hallazgos únicos** (21 críticos) tras la
> verificación adversaria, que refutó cuatro. Además de lo anterior se corrigió:
>
> * `ensure_mounted()` tenía el mismo fallo que el rcd: escribía la unidad con un
>   puerto nuevo y llamaba a `start()`, que en una unidad activa no hace nada —
>   así que el montaje seguía en el puerto viejo mientras `endpoints.json`
>   guardaba el nuevo, y todo lo que consulta el montaje apuntaba a un puerto
>   vacío. Ahora `restart()`.
> * **Fuga de un `QObject` por llamada rc.** Inofensiva mientras nada sondeaba;
>   con los dos sondeos nuevos son miles por hora.
> * Los regex de clasificación: `507|403|429` sueltos casaban con cualquier
>   dígito (`IMG_4290.jpg` → "limitado"), y `\bAUX\b` con la palabra "aux" en
>   cualquier frase. Ahora exigen contexto HTTP y componente de ruta.
> * `_fix_rename` sobrescribía el destino sin comprobar que existiera.
> * `integrity_check()` no se llamaba nunca: una base de datos corrupta bloqueaba
>   el arranque para siempre en vez de repararse sola.
> * **Dos escritores SQLite** sobre el mismo fichero. Ahora se usa el singleton
>   `WRITER` que toda la capa de datos ya buscaba — por eso "No volver a mostrar"
>   no se guardaba nunca.
> * La rotación de `.bak` sobrescribía la copia buena con el fichero corrupto
>   justo después de haber sido rescatado por ella.
> * La pausa no se re-aplicaba en cada tick (lo que su propio docstring exige) y
>   al reanudar no liberaba nada, porque `release()` iba sin endpoint.
> * Nada alimentaba `IssueEngine`: ninguna incidencia se registraba ni se
>   resolvía sola.
> * El Centro de actividad no leía el historial persistido, y el banner de
>   recuperación se emitía a un `Signal` sin suscriptores.
> * La prueba de propiedad del demonio se hacía en cada tick de 2 s (dos llamadas
>   bloqueantes); ahora se cachea 30 s.
> * Abrir Ajustes hacía una llamada rc bloqueante y un recorrido completo de la
>   caché VFS en el hilo de la GUI.
> * "Liberar espacio" mostraba siempre 0 bytes y, sobre una carpeta, no liberaba
>   nada (`evict_tree` no tenía llamador).
> * `mount.enabled` no lo leía nadie; `unlink()` borraba las credenciales sin
>   parar el montaje y anunciaba el borrado aunque fallara.
> * La extensión de Nautilus **descartaba** los avisos `invalidate`.
> * `vacuum_and_prune()` no tenía llamador: las tablas crecían sin límite.
> * **Un fallo en el propio `IOPool` nuevo**: PySide recolectaba el envoltorio
>   del `QRunnable` mientras el hilo seguía dentro de `run()`
>   (`Signal source has been deleted`). Habría saltado al abrir Ajustes.
>
> **Borrado además:** `ui/filebrowser.py` y `platform/thumbnails.py` (1.219
> líneas, sin ningún importador). Un navegador de ficheros propio con su caché de
> miniaturas duplica a Nautilus, con el que ya nos integramos, y no es control de
> rclone.
>
> ### Tercera pasada: los subsistemas sin cablear, resueltos
>
> Cada uno se ha cableado o se ha borrado; ninguno queda a medias.
>
> **Borrada la topología B (bisync).** `rc/bisync.py`, `rc/bisync_log.py`,
> `sync/versions.py`, `sync/conflicts.py` y `sync/watcher.py`. Este cliente
> **monta** el remoto; la carpeta sin conexión mantenía una segunda copia local
> sincronizada en dos sentidos, que es exactamente "crear otra unidad" — algo
> que el usuario prohibió expresamente — no estaba expuesta en ninguna parte de
> la interfaz, no se construía nunca, y era el código con más riesgo de pérdida
> de datos del repositorio. `validate()` ahora **rechaza**
> `offline_folder.enabled`, para que el ajuste no pueda mentir en silencio.
> Recuperable desde git si algún día se quiere la topología B.
>
> **Cableado el asistente.** `Application.start()` lo abre cuando no hay ninguna
> cuenta. Una instalación nueva no mostraba nada en absoluto: ni ventana, ni
> icono, ni forma de iniciar sesión.
>
> **Cableado `log_line`.** Tenía tres emisores y ningún consumidor. Ahora hay un
> **registro en vivo del motor** en la página de rclone, con las últimas 200
> líneas y un botón para abrir el fichero completo — para un cliente cuyo
> trabajo es gobernar rclone, "¿qué está haciendo y qué dijo al fallar?" es la
> pregunta más frecuente, y hasta ahora la única respuesta era `journalctl`.
>
> **Cableado el límite de avisos.** `should_show()`/`note_notification()` no
> tenían llamador, así que el anti-spam de notificaciones vivía sólo en memoria:
> un bucle de reinicios producía un aviso idéntico por arranque.
>
> **Migración 002.** `trg_activity_cap` hacía un anti-join de 5.000 filas en
> **cada** INSERT; ahora barre cada 500 y por rango de clave primaria. Aplicada
> en caliente a la base de datos real (`schema_version = 2`, sin pérdida).
>
> **Señales muertas** (`run_started`, `run_finished`, `vault_state_changed`)
> eliminadas del bus.
>
> **Dos fallos propios, encontrados y corregidos:** el `IOPool` dejaba que
> PySide recolectara el envoltorio del `QRunnable` mientras el hilo seguía
> dentro de `run()`, y tanto el registro nuevo como la medición asíncrona de la
> caché tocaban objetos ya destruidos al cerrar Ajustes. Verificado con cinco
> ciclos completos de construir y destruir la ventana: **cero trazas nuevas**.
>
> ### Cuarta pasada: revisión del propio cambio
>
> Las ~1.900 líneas nuevas no las había revisado nadie, y ya habían producido
> tres fallos encontrados por accidente — todos de la misma familia: **vida de
> los objetos Qt/PySide entre hilos y al destruirse**. Revisándolas a propósito
> aparecieron dos más:
>
> * `Application.quit()` llamaba a `engine.supervisor.stop()`, **no** a
>   `engine.stop()`, así que el desmontaje de los sondeos nuevos no se ejecutaba
>   nunca y el `IOPool` no se drenaba (§7.5 paso 2). Corregido: ahora sigue el
>   orden de §7.5.
> * `Engine.stop()` paraba el escritor, que ahora es el singleton compartido —
>   con dos cuentas, la primera en pararse cerraba la conexión que la otra
>   seguía usando. El ciclo de vida del escritor pertenece a la aplicación.
>
> **Verificado funcionalmente**, no sólo leído: el `IOPool` con cinco ciclos
> completos de construir y destruir la ventana de Ajustes (cero trazas); la poda
> ejecutándose *dentro* de la transacción del escritor; el ámbito por cuenta de
> `Config.get/set` (y que el valor por defecto sigue siendo el de antes); el
> puente aviso→banner de extremo a extremo; el proveedor de estados de IPC con
> rutas dentro y fuera de la raíz; la guarda I1 sobre el campo de argumentos
> libres; `mount.enabled`; y que la poda selectiva evita la papelera local
> dentro del montaje — que habría propagado el borrado a la nube.
>
> ### Quinta pasada: auditoría adversaria del propio cambio
>
> Siete revisores sobre las ~2.400 líneas nuevas, por áreas. Encontraron **35
> defectos en mi propio trabajo**, y son la parte más valiosa de la sesión.
> Los que más importan:
>
> * **La página de rclone no aplicaba nada.** "Aplicar — reiniciar el montaje"
>   reiniciaba la unidad *vieja*: el argv sólo vive en el fichero de unidad, y el
>   único sitio que lo escribe es `ensure_mounted()`, que sale antes si el
>   montaje está UP. Ahora `MountController.rewrite_unit()` lo vuelve a generar
>   —reutilizando puerto y credenciales, para no dejar a los sondeos apuntando
>   a un puerto muerto— antes de reiniciar.
> * **Una subida de versión condenaba a nuestro propio demonio.** La prueba de
>   propiedad exigía el `USER_AGENT` completo con versión; el demonio sobrevive a
>   las actualizaciones con el argv con el que arrancó, así que la primera
>   actualización lo habría marcado FOREIGN para siempre. Ahora se compara el
>   prefijo sin versión.
> * **Un `core/pid` lento se convertía en 30 s de FOREIGN**, porque la caché de
>   la prueba guardaba también el veredicto negativo.
> * **Las tres pausas automáticas no diferían nada**: `enforce()` se cerraba
>   sobre `active()`, que sólo refleja una pausa manual.
> * **Alimentar el motor de incidencias en cada tick** costaba ~10 viajes
>   bloqueantes a SQLite cada dos segundos en el hilo de la GUI. Ahora sólo
>   cuando cambia alguno de los hechos que leen.
> * **La detección de "sin conexión" informaba de éxito siempre**, porque
>   `quota.refresh()` se traga sus propias excepciones; ahora se mira si la
>   muestra avanzó.
> * El ancho de banda era una foto fija del arranque; el asistente se abría sin
>   los servicios que necesita; el `IpcServer` se construía por cuenta sobre un
>   socket que es del proceso; `integrity_check` —que aparta una base de datos
>   que juzga corrupta— corría **antes** del guardián de instancia única.
> * En el `IOPool`: fuga de tokens, fallos silenciosos sin `on_error`, `progress`
>   nunca entregado, y las señales de las tareas descartadas sin liberar.
>
> Todos corregidos y verificados. Quedaron sin tocar sólo dos observaciones que
> resultaron **falsas** al leerlas contra el código: el orden de
> `TERMINAL_RULES` ya protege el aborto por `--max-delete`, y PySide sí honra
> la sustitución de un virtual por atributo de instancia.
>
> **La suite no se ha ejecutado** desde estos cambios, por indicación expresa.
> Hay que actualizarla: se borraron tres módulos (y `tests/test_kfm.py`), y
> cambiaron la prueba de propiedad, la firma de `Config.get/set`, `_provision()`
> y `open_activity()`.

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
