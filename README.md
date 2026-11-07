# 🩺 Crafty Medic

> Il bottone "il mio server Minecraft non parte più" per chi usa **Crafty Controller** su **ZimaOS**.

Crafty Medic è un piccolo container che si aggancia alla tua istanza di Crafty Controller, legge il log del
server che è andato in crash, **capisce quale mod o plugin è il colpevole** e te lo propone da rimuovere o
aggiornare **con un click** — con backup, quarantena e rollback automatico se qualcosa va storto.

L'obiettivo è l'esperienza di Aternos ("c'è un problema → clicchi → riparte"), ma in self-hosted e senza
cancellare nulla.

---

## Perché un container esterno e non un "plugin di Crafty"

Crafty Controller 4 **non ha un sistema di plugin**: non esiste una cartella dove caricare estensioni né
un'API per aggiungere schede alla sua interfaccia. Quello che espone è una **REST API v2** completa
(server, log, comandi, backup) e i file dei server sul disco.

Crafty Medic usa esattamente questi due canali:

```
┌──────────────────────┐        API v2 (HTTPS)         ┌────────────────────────┐
│  Crafty Medic        │ ───────────────────────────►  │  Crafty Controller     │
│  (container sidecar) │   lista server, log, start,    │  (container su ZimaOS) │
│                      │   stop, restart, backup        │                        │
│                      │                                │                        │
│  interfaccia web     │ ◄── volume condiviso (rw) ───► │  /crafty/servers/...   │
│  :8765               │   legge e sposta mods/plugins  │                        │
└──────────────────────┘                                └────────────────────────┘
```

Quindi: **nessuna modifica a Crafty**, nessun file di Crafty toccato, si disinstalla cancellando un container.

---

## Cosa sa fare

| Funzione | Dettaglio |
|---|---|
| **Diagnosi dal log** | ~30 regole per Fabric, Forge/NeoForge, Paper/Spigot/Bukkit, JVM e configurazione |
| **Diagnosi senza log** | scansione dei metadati dentro i jar (`fabric.mod.json`, `mods.toml`, `plugin.yml`) per trovare mod/plugin incompatibili anche a server spento |
| **Quarantena (1 click)** | il file viene spostato in una cartella sicura, **mai cancellato** |
| **Aggiornamento (1 click)** | cerca su **Modrinth** la versione compatibile con la tua MC + loader e sostituisce il jar |
| **Spostamento (1 click)** | plugin finito in `mods/`, mod finito in `plugins/`, jar perso in una sottocartella |
| **Backup automatico** | prima di ogni intervento salva `mods/`, `plugins/` e i file di configurazione |
| **Verifica e rollback** | riavvia il server, rilegge il log e, se il crash persiste, rimette tutto come prima |
| **Storico** | registro di tutte le operazioni e ripristino selettivo dalla quarantena |

### Esempi di errori riconosciuti

* `Mod 'X' (x) requires version 0.5.x of mod sodium, which is missing!` → dipendenza mancante
* `requires version 1.21.x of minecraft, which is missing!` → mod per un'altra versione di Minecraft
* `Missing or unsupported mandatory dependencies: Mod ID: 'jei', Requested by: 'derivedaddon'` → Forge
* `Unsupported API version 1.13` → plugin vecchio su Paper recente
* `Unknown/missing dependency plugins: [Vault]` → plugin senza la sua dipendenza
* `InvalidPluginException: Invalid plugin.yml` → jar sbagliato o corrotto
* `has been compiled by a more recent version of the Java Runtime (class file version 61.0)` → Java sbagliato
* `You need to agree to the EULA`, `FAILED TO BIND TO PORT`, `OutOfMemoryError`, mondo danneggiato
* Mod solo client su server, mod duplicati, jar corrotti, mod/plugin nella cartella sbagliata

---

## Installazione su ZimaOS (5 minuti)

### 1. Genera il token API di Crafty

1. Apri Crafty nel browser, clicca l'**ingranaggio** in alto a destra
2. Seleziona il tuo utente → **API Keys** → **Generate Key**
3. Copia il token (serve un utente amministratore: i token limitati non vedono i log)

### 2. Prepara la cartella

Sullo ZimaOS (via SSH o dal file manager):

```bash
mkdir -p /DATA/AppData/crafty-medic/data
cd /DATA/AppData/crafty-medic
# copia qui i file del progetto (Dockerfile, docker-compose.yml, app/, scripts/, requirements.txt)
```

### 3. Monta lo stesso volume dei server

Questo è il passaggio **fondamentale**: Crafty Medic deve vedere le cartelle dei server
nello stesso punto in cui le vede Crafty, ma in lettura+scrittura.

Esempio: se nel `docker-compose.yml` di Crafty trovi

```yaml
    volumes:
      - /DATA/AppData/crafty/servers:/crafty/servers
```

allora in Crafty Medic devi mettere

```yaml
      - /DATA/AppData/crafty/servers:/mnt/crafty/servers:rw
    environment:
      CRAFTY_SERVERS_DIR: "/mnt/crafty/servers"
      CRAFTY_MEDIC_PATH_MAP: "/crafty/servers=/mnt/crafty/servers"
```

`CRAFTY_MEDIC_PATH_MAP` traduce i path che Crafty riporta via API (`/crafty/servers/<uuid>`)
nei path visti da questo container. Se Crafty monta i server altrove (es. `/crafty/app/servers`),
adatta la mappa.

### 4. Configura `.env`

```bash
cp .env.example .env
nano .env
```

Valori minimi:

```dotenv
CRAFTY_URL=https://192.168.1.10:8443      # IP di ZimaOS : porta di Crafty
CRAFTY_TOKEN=il-tuo-token
CRAFTY_SERVERS_DIR=/mnt/crafty/servers
CRAFTY_MEDIC_PATH_MAP=/crafty/servers=/mnt/crafty/servers
```

### 5. Avvia

```bash
cd /DATA/AppData/crafty-medic
docker compose up -d --build
```

Apri **http://IP-DI-ZIMAOS:8765**

> Su ZimaOS puoi anche creare una "Custom app" incollando direttamente il contenuto di
> `docker-compose.yml` (adattando i path).

### Permessi (opzionale)

Se Crafty gira con un utente non-root, allinea Crafty Medic con `PUID` e `PGID`
(gli stessi che usi per Crafty), così i file spostati/restaurati restano suoi.

---

## Come si usa

1. Dalla **dashboard** clicca **Analizza e ripara** sul server che non parte
2. Crafty Medic mostra i problemi trovati, con:
   * il motivo spiegato in italiano,
   * le righe del log che lo dimostrano,
   * il file colpevole e l'azione proposta (quarantena / aggiornamento / spostamento)
3. Le azioni sicure sono già spuntate: premi **🛠 Ripara ora**
4. Parte la sequenza: *backup → fermo server → sposto i file → riavvio → verifica*
5. Se dopo il riavvio il crash c'è ancora → **rollback automatico** e te lo dice
6. Vuoi tornare indietro a mano? Vai in **Quarantena** → **Ripristina**

L'aggiornamento da Modrinth è "best effort": se il nome del mod non basta a identificare il progetto
giusto, il tool te lo dice e puoi indicare a mano lo slug in **Impostazioni → Abbinamenti Modrinth**
(es. `sodium-fabric-1.20.1-0.5.3.jar` → `sodium`).

---

## Variabili d'ambiente

| Variabile | Default | Descrizione |
|---|---|---|
| `CRAFTY_URL` | – | URL di Crafty (es. `https://192.168.1.10:8443`) |
| `CRAFTY_TOKEN` | – | token API Crafty (utente amministratore) |
| `CRAFTY_VERIFY_SSL` | `false` | Crafty usa certificati autofirmati |
| `CRAFTY_SERVERS_DIR` | – | cartella dei server vista da questo container |
| `CRAFTY_MEDIC_PATH_MAP` | – | traduzioni `pathCrafty=pathLocale` separate da virgola |
| `CRAFTY_MEDIC_DATA_DIR` | `/data` | quarantena, backup, registro |
| `CRAFTY_MEDIC_BACKUP` | `true` | backup prima di ogni riparazione |
| `CRAFTY_MEDIC_AUTO_ROLLBACK` | `true` | annulla se il server non riparte |
| `CRAFTY_MEDIC_BOOT_WAIT` | `60` | secondi per la verifica dell'avvio |
| `CRAFTY_MEDIC_UPDATER` | `true` | ricerca aggiornamenti su Modrinth |
| `CRAFTY_MEDIC_DEMO` | `false` | tre server finti con crash finti, per provare senza rischi |
| `CRAFTY_MEDIC_HOST` / `CRAFTY_MEDIC_PORT` | `0.0.0.0` / `8765` | interfaccia web |
| `PUID` / `PGID` | `0` | utente con cui girare |

---

## Provalo senza Crafty (demo)

```bash
git clone https://github.com/AleCl1210/FixErrorsInCraftyController.git
cd FixErrorsInCraftyController
pip install -r requirements.txt
CRAFTY_MEDIC_DEMO=true python3 scripts/make_demo.py --force
CRAFTY_MEDIC_DEMO=true CRAFTY_MEDIC_DATA_DIR=./data python -m app
# http://localhost:8765
```

Tre server finti (Fabric 1.20.1, Paper 1.20.6, Forge 1.20.1) con crash realistici: puoi provare
quarantena, spostamenti e rollback senza toccare nulla di tuo.

---

## Sicurezza

* **Niente viene cancellato**: i file finiscono in quarantena e si ripristinano con un click
* **Backup prima di agire**: `mods/`, `plugins/` e configurazioni vengono archiviati in `data/backups/`
* **Rollback automatico** se dopo la riparazione il server non si avvia
* **Nessun comando eseguito sul server**: Crafty Medic non scrive in console né nel mondo
* Il token API va trattato come una password: Crafty Medic lo usa solo verso la tua istanza
* L'aggiornamento da Modrinth scarica un jar e lo sostituisce: il vecchio resta in quarantena

---

## Limiti noti

* **Bedrock** non è supportato (solo Minecraft Java)
* **CurseForge** richiede una API key personale: per ora gli aggiornamenti arrivano da **Modrinth**
* L'aggiornamento automatico è euristico: controlla sempre che il progetto trovato sia quello giusto
  (il tool mostra nome, versione e link prima/dopo l'operazione)
* Alcuni problemi non sono riparabili spostando file (Java sbagliato, porta occupata, RAM insufficiente,
  mondo corrotto): vengono segnalati con le istruzioni per sistemarli a mano
* La diagnosi dipende dal log: se Crafty lo ruota subito, rilancia l'analisi dopo un tentativo di avvio

---

## Struttura del progetto

```
app/
  analyzer.py    # motore di diagnosi (log + scansione jar)
  rules.py       # ~30 regole di riconoscimento errori
  jarmeta.py     # lettura di fabric.mod.json / mods.toml / plugin.yml
  versions.py    # confronto versioni e range (1.20.x, [1.20.1,1.21), ...)
  repair.py      # quarantena, spostamenti, aggiornamenti, verifica, rollback
  updater.py     # client Modrinth
  crafty.py      # client API v2 di Crafty Controller
  store.py       # quarantena, backup locali, registro operazioni
  web.py         # interfaccia web (Flask)
  demo.py        # provider finto per la modalità demo
scripts/make_demo.py  # genera i dati dimostrativi
tests/                # pytest
```

## Sviluppo

```bash
pip install -r requirements.txt
python -m pytest tests -q
CRAFTY_MEDIC_DEMO=true python -m app
```

Per aggiungere il riconoscimento di un nuovo errore basta una regola in `app/rules.py`
(pattern, categoria, rimedio e funzione che estrae il nome del mod/plugin dal log)
e un test in `tests/test_analyzer.py`.
