# 🩺 Crafty Medic

> Il modo rapido per far ripartire un server Minecraft che si è rotto: trova il mod o il plugin
> colpevole e te lo toglie / aggiorna con un click (o con un comando).

Pensato per chi usa **Crafty Controller su ZimaOS**. Due modi di usarlo:

| | **A. Dentro il container di Crafty** | **B. Container a parte con interfaccia web** |
|---|---|---|
| Come si usa | dal terminale che ZimaOS apre sui 3 puntini dell'app Crafty | pagina web su `http://IP-ZIMAOS:8765` |
| Cosa installi | un solo file `.py` (nessuna dipendenza) | un container Docker in più |
| Riavvia il server | te lo chiede (lo fai da Crafty) | lo fa da solo, verifica e annulla se non riparte |
| Ideale se | vuoi zero sbatti e zero container in più | vuoi i bottoni e la riparazione automatica |

**Crafty Controller 4 non ha un sistema di plugin** (non accetta estensioni), quindi nessuna delle
due strade modifica Crafty: entrambe leggono i log e spostano file.

---

# A. Dentro il container di Crafty (metodo veloce)

## 1. Apri il terminale

Da ZimaOS: **icona di Crafty → tre puntini → Terminale**. Sei *dentro* il container.

## 2. Scarica lo script (una volta sola)

```sh
cd /crafty/servers
curl -sSL -o crafty-medic.py https://raw.githubusercontent.com/AleCl1210/FixErrorsInCraftyController/arena/849627e8-fixerrorsincraftycontroller/crafty-medic.py
```

> **Perché proprio in `/crafty/servers`?** È una cartella "montata" (volume): sopravvive se ZimaOS
> ricrea o aggiorna il container. Tutto il resto del container viene azzerato.
> Se salvassi lo script in `/root` o `/opt`, al primo aggiornamento dell'app lo perderesti.

**Se il container non ha internet** (il comando sopra dà errore): scarica `crafty-medic.py` dal PC,
poi caricalo con il file manager di Crafty dentro la cartella di un server qualsiasi, e lancialo
da lì: `python3 /crafty/servers/<tuo-server>/crafty-medic.py`. Oppure incollalo a mano:

```sh
cat > /crafty/servers/crafty-medic.py <<'FINE'
    # incolla qui dentro tutto il contenuto del file crafty-medic.py
FINE
```

## 3. Usalo

```sh
python3 /crafty/servers/crafty-medic.py            # menu: scegli il server e ripara
```

Oppure i comandi diretti:

```sh
python3 /crafty/servers/crafty-medic.py scan                 # analizza tutti i server
python3 /crafty/servers/crafty-medic.py scan demo-1          # solo uno
python3 /crafty/servers/crafty-medic.py fix demo-1           # ti chiede cosa fare
python3 /crafty/servers/crafty-medic.py auto demo-1          # sistema tutto da solo (vedi sotto)
python3 /crafty/servers/crafty-medic.py watch                # sorveglia i log e ripara da sé
python3 /crafty/servers/crafty-medic.py undo                 # annulla l'ultima riparazione
python3 /crafty/servers/crafty-medic.py quarantine           # elenca i file tolti
python3 /crafty/servers/crafty-medic.py history              # cosa è stato fatto
```

### `auto`: la regola è "aggiorna se puoi, togli se non puoi"

`auto demo-1` (uguale a `fix demo-1 --auto`) applica questa logica a ogni problema:

1. **Se il mod/plugin è obsoleto** (versione sbagliata di Minecraft, API vecchia…) → cerca su Modrinth
   una versione compatibile con la tua versione + loader e **la scarica al posto del vecchio**
2. **Se l'aggiornamento non è possibile** (progetto non trovato, nessuna versione compatibile,
   container senza internet, nome ambiguo) → **mette il file in quarantena**, così il server riparte
3. Se il problema è solo di posizione (es. un plugin in `mods/`) → **lo sposta** nella cartella giusta

Prima di toccare anything fa un backup; se poi il server non riparte, `undo` rimette tutto a posto.
Con `--no-update` salta il punto 1 e toglie direttamente.

### `watch`: zero click, come Aternos

```sh
cd /crafty/servers && nohup python3 crafty-medic.py watch > watch.log 2>&1 &
```

Resta in ascolto dentro il container: **ogni 30 secondi** (regolabile con `--every 10`) controlla i log
di tutti i server e, appena vede un crash, ripara da solo con la stessa regola di `auto`
(aggiorna, o toglie). Se in Crafty hai attivo il riavvio automatico del server, te lo ritrovi su
senza aver fatto niente.

* `watch --once` → fa un solo controllo ed esce (comodo anche per il cron)
* `watch demo-1 --every 10` → solo un server, ogni 10 secondi
* `cat watch.log` per vedere cosa ha fatto; `kill %1`/`pkill -f crafty-medic` per fermarlo
* ⚠️ essendo dentro il container, se ZimaOS ricrea il container il watch si ferma:
  ti basta rilanciare quel comando (o metterlo tra i preferiti)

Se vuoi i bottoni invece dei comandi, c'è la **sezione B**: stesso motore, con pulsanti,
riavvio e rollback automatici.

## Che cosa vedi

```
Server: Moddato Fabric 1.20.1  (/crafty/servers/demo-fabric)
    Minecraft: 1.20.1 | loader: fabric | jar installati: 5 | fermo

[1] Mod per un'altra versione di Minecraft
    Questo mod non supporta la versione di Minecraft del server: aggiornalo o toglilo.
    severita': critico | affidabilita': 95% | da: log
    file: mods/jei-1.21.1.jar
    ---
     - Mod 'JEI' (jei) 15.2.0.27 requires version 1.21.x of minecraft, which is missing!
    ---

[2] Dipendenza mancante
    file: mods/reeses-sodium-options-1.6.4.jar
    ---
     - Mod 'Reese's Sodium Options' (reeses-sodium-options) 1.6.4 requires version 0.5.x
       of mod sodium, which is missing!
    ---

    per riparare: python3 /crafty/servers/crafty-medic.py fix demo-fabric
```

Poi `fix` ti mostra la lista numerata e tu rispondi con i numeri, come preferisci:

| Cosa scrivi | Cosa ripara |
|---|---|
| `1-3` | dal problema 1 al 3 |
| `1,3` | solo il problema 1 e il 3 |
| `2-` | dal 2 fino alla fine |
| `-3` | dall'inizio fino al 3 |
| `1,3-5,8` | misto |
| `tutti` (o `*`, o `all`) | tutti |
| Invio | annulla senza toccare nulla |

Con `--auto` salta la domanda e ripara tutto.

```
  ✓ backup: /crafty/backups/crafty-medic/backups/demo-paper-20261107-203326.tar.gz
  ✓ in quarantena: plugins/EssentialsX-2.19.0.jar
  ✓ in quarantena: plugins/ShopGUI.jar
  ✓ spostato plugins/sodium-fabric-1.20.1-0.5.3.jar -> mods/

  ! avvia il server da Crafty per verificare
    se non riparte: python3 /crafty/servers/crafty-medic.py undo
```

## Cosa fa, cosa non fa

* **Non cancella mai niente**: i file tolti vanno in quarantena, `undo` li rimette al loro posto
  (anche gli spostamenti vengono annullati).
* **Backup automatico** di `mods/`, `plugins/` e file di configurazione prima di ogni riparazione.
* **Non riavvia il server da solo** (da dentro il container non può farlo): te lo chiede alla fine.
  Dopo il riavvio rilancia `scan` per controllare che sia tutto ok.
* Aggiornamento automatico da Modrinth: `update demo-1 --file mods/jei.jar [--project jei]`
  (serve internet nel container).

## Se qualcosa non torna

| Messaggio | Cosa fare |
|---|---|
| `nessun server trovato in /crafty/servers` | indica la cartella giusta: `--dir /percorso/corretto` |
| `nessun log trovato (logs/latest.log)` | avvia il server una volta da Crafty, poi rilancia `scan` |
| `Minecraft: ? | loader: ?` | idem: senza log non posso indovinare la versione |
| il server è `IN ESECUZIONE` | fermarlo prima è più pulito, ma lo script funziona lo stesso |

---

# B. Container a parte con interfaccia web

Un piccolo container che si aggancia a Crafty via API, con pulsanti, verifica dell'avvio
e rollback automatico.

```
┌──────────────────────┐        API v2 (HTTPS)         ┌────────────────────────┐
│  Crafty Medic        │ ───────────────────────────►  │  Crafty Controller     │
│  (container sidecar) │   lista server, log, start,   │  (container su ZimaOS) │
│                      │   stop, restart, backup       │                        │
│  interfaccia web     │ ◄── volume condiviso (rw) ───►│  /crafty/servers/...   │
│  :8765               │   legge e sposta mods/plugins  │                        │
└──────────────────────┘                                └────────────────────────┘
```

## Installazione su ZimaOS

1. **Token API**: Crafty → ingranaggio → tuo utente → **API Keys** → Generate (serve un admin)
2. **Cartella**: `mkdir -p /DATA/AppData/crafty-medic/data` e copiaci i file del progetto
3. **Volume**: rimonta la stessa cartella host dei server di Crafty, in `rw`.
   Se Crafty ha `- /DATA/AppData/crafty/servers:/crafty/servers`, in Crafty Medic metti:

```yaml
      - /DATA/AppData/crafty/servers:/mnt/crafty/servers:rw
    environment:
      CRAFTY_SERVERS_DIR: "/mnt/crafty/servers"
      CRAFTY_MEDIC_PATH_MAP: "/crafty/servers=/mnt/crafty/servers"
```

4. `cp .env.example .env` e compila `CRAFTY_URL`, `CRAFTY_TOKEN`, `CRAFTY_SERVERS_DIR`
5. `docker compose up -d --build` → apri **http://IP-DI-ZIMAOS:8765**

Dalla scheda del server hai due strade:

* **🩺 Sistema tutto da solo** (un click): per ogni problema **aggiorna** il mod/plugin se Modrinth ha
  una versione compatibile, altrimenti lo **mette in quarantena**; poi fa backup → ferma → applica →
  riavvia → rilegge il log → **se il crash c'è ancora, annulla tutto**.
* **🛠 Ripara ora** (scegli tu): ogni problema ha le sue opzioni spuntate
  (*Aggiorna*, *Metti in quarantena*, *Sposta in plugins/*) e decidi cosa applicare.

---

# La regola automatica (in entrambi i modi)

Per ogni problema trovato, Crafty Medic prova nell'ordine:

1. **Aggiorna** — se il mod/plugin è obsoleto e su Modrinth esiste una versione compatibile
   con la tua versione di Minecraft e il tuo loader, la scarica e la sostituisce (il vecchio
   va in quarantena)
2. **Sposta** — se il file è solo nella cartella sbagliata (`plugins/` invece di `mods/`,
   o in una sottocartella), lo mette dove va
3. **Quarantena** — se non si può aggiornare né spostare, lo toglie (senza cancellarlo) così il server riparte

Sempre con backup prima, e sempre annullabile.

# Cosa sa riconoscere

| Categoria | Esempi |
|---|---|
| **Mod (Fabric)** | `requires version 0.5.x of mod sodium, which is missing!`, `requires version 1.21.x of minecraft, which is missing!`, `is incompatible with`, mod duplicati, mod solo client, errore di entrypoint |
| **Mod (Forge/NeoForge)** | `Missing or unsupported mandatory dependencies: Mod ID: 'jei', Requested by: 'x'`, `Mixin apply for mod x failed`, `is missing mods.toml`, errore di fase di caricamento |
| **Plugin (Paper/Spigot)** | `Unsupported API version 1.13`, `Unknown/missing dependency plugins: [Vault]`, `Invalid plugin.yml`, `Error occurred while enabling X (Is it up to date?)`, `Ambiguous plugin name` |
| **Java / server** | `UnsupportedClassVersionError`, Java troppo vecchio, `OutOfMemoryError`, EULA non accettata, porta occupata, jar del server mancante, mondo danneggiato |
| **Solo da scansione file** | mod per un'altra versione di Minecraft, dipendenze non installate, jar corrotti, mod duplicati, plugin in `mods/` (o viceversa), jar persi in sottocartelle, `api-version` più nuova del server |

La **scansione dei file** legge cosa c'è scritto dentro ogni jar (`fabric.mod.json`, `mods.toml`,
`plugin.yml`), quindi funziona **anche senza log** e a server spento.

---

# Come si usa in pratica (il flusso consigliato)

1. Il server non parte → apri il terminale (o la pagina web) e lancia l'**analisi**
2. Leggi i problemi: ognuno ha il motivo in italiano, la riga di log che lo dimostra e il file
3. Applica solo quelli che ti convincono (le azioni "sicure" sono già proposte)
4. Riavvia da Crafty
5. Se va: fine. Se non va: `undo` (o rollback automatico nella versione web) e riprovi con un altro file

---

# Sicurezza e limiti

* Nulla viene cancellato: quarantena + backup + `undo`
* Nessun comando viene inviato al server Minecraft, nessun file del mondo toccato
* L'aggiornamento da Modrinth è euristico: se non è sicuro del progetto giusto te lo dice e ti
  chiede lo slug (es. `--project sodium`)
* **CurseForge richiede una API key personale**: per ora gli aggiornamenti arrivano da Modrinth
* Bedrock non è supportato
* Alcuni problemi non si risolvono spostando file (Java sbagliato, RAM, porta, mondo corrotto):
  vengono segnalati con le istruzioni per sistemarli a mano

---

# Sviluppo

```bash
pip install -r requirements.txt
python -m pytest tests -q          # 34 test
CRAFTY_MEDIC_DEMO=true python3 scripts/make_demo.py --force
CRAFTY_MEDIC_DEMO=true python -m app        # interfaccia web su :8765

# oppure lo script singolo contro la demo:
python3 crafty-medic.py scan --dir demo --home /tmp/cm
```

Per riconoscere un nuovo errore basta aggiungere una regola in `app/rules.py` (interfaccia web)
e in `RULES` dentro `crafty-medic.py` (script a riga di comando), più un test in `tests/`.
