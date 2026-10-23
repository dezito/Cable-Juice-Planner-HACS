<img src="custom_components/cable_juice_planner/brand/icon.png" alt="Cable Juice Planner" width="128" align="right">

# 🚗Cable Juice Planner🔋🌞📅 til Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)
[![GitHub Release](https://img.shields.io/github/v/release/dezito/Cable-Juice-Planner-HACS)](https://github.com/dezito/Cable-Juice-Planner-HACS/releases)
[![License](https://img.shields.io/github/license/dezito/Cable-Juice-Planner-HACS)](LICENSE)

Planlægger opladning af elbilen ud fra en arbejdsplan eller de billigste tider over en uge.
Planen bruger elpriser, solcelleoverskud, husbatteri og bilens faktiske forbrug.

> [!NOTE]
> Dette er **integrationsudgaven** af Cable Juice Planner, som installeres via HACS. Den erstatter
> [PyScript-udgaven](https://github.com/dezito/Cable-Juice-Planner) og kan importere dens
> konfiguration og databaser. Se [Skift fra PyScript-udgaven](#skift-fra-pyscript-udgaven).

> [!IMPORTANT]
> Integrationen **sender ingen kommandoer** til laderen eller bilen. Planen udstilles som
> [handlings-entiteter](#sådan-styrer-du-laderen-og-bilen), og du styrer selv laderen med en
> automation. Til det følger der en [blueprint](#blueprint-styr-laderen) med fail-safe med.

<a href="https://sloth.nu/tesla_solar_demo.gif" target="_blank"><img src="images/tesla_solar_demo_small.gif" width="49%"></a><a href="https://sloth.nu/virtual_ev_demo.gif" target="_blank"><img src="images/virtual_ev_demo_small.gif" width="49%"></a>

![Opladningsperiode](images/charging_period.png)

---

## Ladereglerne

- **☀️ Solcelleoverskud til opladning**
  - Bilen lades med solcellernes overskudsproduktion. Den kan bruge hele timens overskud, også selvom
    opladningen først starter senere i timen.
  - Kan også indstilles til X minutter tilbage. Med f.eks. 15 min deles timen op i fire
    (13:00, 13:15, 13:30 og 13:45), og overskuddet bruges i de intervaller.
- **🏢 Arbejdsplan opladning**
  - Angiv antal kilometer, afgang, hjemkomst og forvarmning pr. arbejdsdag. Integrationen finder de
    billigste tider at lade på og forvarmer bilen til afgang, hvis det er valgt.
- **📅 Optimal ugeopladning (uden arbejdsplan)**
  - Angiv hvor mange kilometer du typisk kører om dagen. Forskellen mellem det nuværende
    batteriniveau (f.eks. 27 %) og **elbilens maks. anbefalede ladeprocent** (f.eks. 90 %) deles op i
    tre dele (63 % / 3 = 21 %). Integrationen lader så i de billigste tider 1, 4 og 7 dage frem.
- **🧳 Tur:** lad til en procent eller til et antal km før en afgang, evt. med forvarmning
- **🔌 Manuel ladning** (f.eks. gæster), evt. kun på solcelleoverskud
- **🪫 Tvangsladning under dagligt batteriniveau** (op til f.eks. 30 %), men ikke i de dyreste timer
- **💸 Billig og meget billig strøm:** lad automatisk højere op, når strømmen er ekstra billig

> [!NOTE]
> Er alle laderegler slået fra, lader Cable Juice Planner som en almindelig lader med maks. strøm.

## Funktioner

- **Elbil-integration eller virtuel elbil:** uden bil i Home Assistant indtaster du selv
  batteriniveauet, når kablet sættes i
- **Solceller og Solcast (valgfrit):** overskud nu og prognose fremover, fra
  [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar) og/eller egen historik
- **Husbatteri (valgfrit):** lad husbatteriet til et bestemt niveau først, og lad bilen fra
  husbatteriet over et minimum
- **Flere biler på samme hovedsikring:** [lastfordeling](#flere-biler-lastfordeling) med prioritet og fasefølge
- **Husets forbrug** estimeres ud fra egen historik (uden bil, husbatteri og ignorerede forbrugere)
- **Prisudbydere:** [Energi Data Service](https://github.com/MTrab/energidataservice) og/eller
  [Strømligning](https://github.com/MTrab/stromligning), inkl. salgspris, tariffer og refusion af elafgift
- **Realistisk forbrug** ud fra kørselsmønstret: kørselseffektivitet, km/kWh, Wh/km og estimeret rækkevidde
- **Offentlig opladning:** registreres automatisk (med bil-integration). Du indtaster prisen
  bagefter, og sessionen huskes også efter en genstart.
- **Ladetab:** kan beregnes automatisk og gemmes
- **CO₂-udledning** og **kWh ladet af solcellerne**
- **Meget detaljeret ladehistorik**
  - hver ladning: tid, ladegrund, procent, kWh, pris og kr/kWh
  - pr. måned: km, kWh, andel fra solceller, pris og kr/kWh
  - fordeling mellem dag og nat
  - offentlig ladning
- **Oversigter:** sidste køreturs effektivitet, udgifter for nuværende batteriniveau,
  ladeoversigt, afgangsplan, skøn ved daglig opladning og solcelleoverskud-prognose
- **Fail-safe:** se [Fail-safe](#fail-safe)
- **Færdigt dashboard** i sidepanelet
- **Sprog:** dansk, engelsk, tysk, polsk og ukrainsk
- **Kører ikke i Home Assistants hovedtråd:** alle beregninger kører i integrationens egen
  worker-tråd, så Home Assistant ikke blokeres mens planen regnes ud

| Sidste køretur | Batteriniveau-udgifter | Ladeoversigt |
| --- | --- | --- |
| ![Sidste kørsel effektivitet](images/last_drive_efficiency.png) | ![Batteriniveau-udgifter](images/battery_level_costs.png) | ![Ladeoversigt](images/charging_overview.png) |

| Oversigt | Historik | Solcelleoverskud |
| --- | --- | --- |
| ![Oversigt](images/overview.png) | ![Historik](images/history.png) | ![Solcelleoverskud](images/solar_overproduction.png) |

---

## Krav

- Home Assistant **2025.1** eller nyere med recorder (standard)
- En lader med **status**-, **effekt**- (W) og **kWh**-sensorer
- Prisintegration: [Energi Data Service](https://github.com/MTrab/energidataservice) og/eller [Strømligning](https://github.com/MTrab/stromligning)
- Valgfrit:
  - bil-integration (batteriniveau, rækkevidde og kilometertæller, og gerne ladeport, ladegrænse og position)
  - solcelleproduktion (W) og [Solcast PV Forecast](https://github.com/BJReplay/ha-solcast-solar)
  - en vejr-integration med `weather.get_forecasts`, f.eks.
    [Meteorologisk institutt](https://www.home-assistant.io/integrations/met/),
    [AccuWeather](https://www.home-assistant.io/integrations/accuweather/) eller
    [OpenWeather](https://www.home-assistant.io/integrations/openweathermap/)
- Til dashboardet (HACS frontend): `mushroom`, `stack-in-card`, `numberbox-card`,
  `apexcharts-card`, `config-template-card` og `ultra-vehicle-card`

### Testet med

| Lader | Bil | Inverter |
|---|---|---|
| [Easee](https://github.com/nordicopen/easee_hass) | [Tesla](https://github.com/alandtse/tesla), [TeslaFi](https://github.com/jhansche/ha-teslafi), [Tessie](https://www.home-assistant.io/integrations/tessie/), [Polestar](https://github.com/pypolestar/polestar_api), [Cupra WeConnect](https://github.com/daernsinstantfortress/cupra_we_connect), [Kia UVO](https://github.com/Hyundai-Kia-Connect/kia_uvo) | [Huawei Solar](https://github.com/wlcrs/huawei_solar), [Fronius](https://www.home-assistant.io/integrations/fronius/), [Kostal](https://www.home-assistant.io/integrations/kostal_plenticore/), [Solarman](https://github.com/StephanJoubert/home_assistant_solarman/) |

Laderen styres af din egen automation, så andre ladere virker også, hvis de kan startes,
stoppes og have ampere sat fra Home Assistant.

---

## Installation

### HACS (anbefalet)

[![Åbn dit Home Assistant og tilføj repoet i HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=dezito&repository=Cable-Juice-Planner-HACS&category=integration)

1. **HACS** → ⋮ → **Custom repositories**: tilføj `https://github.com/dezito/Cable-Juice-Planner-HACS` som **Integration**.
2. Søg efter **Cable Juice Planner**, installer den, og genstart Home Assistant.
3. Tilføj integrationen:

   [![Åbn dit Home Assistant og tilføj Cable Juice Planner.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=cable_juice_planner)

   eller **Indstillinger → Enheder og tjenester → Tilføj integration → Cable Juice Planner**.
4. Lav automationen, der styrer laderen, f.eks. med [blueprinten](#blueprint-styr-laderen).

### Manuelt

Kopiér `custom_components/cable_juice_planner` til `config/custom_components/cable_juice_planner`, og genstart Home Assistant.

---

## Opsætning

Opsætningen sker i brugerfladen, fordelt på sider med hjælpetekster:

1. **Bil:** navn (bruges som enhedens navn og i notifikationer), prefix og sprog. Alle entity
   id'er starter med `cjp_`. Uden prefix hedder de `sensor.cjp_overview`, og med prefixet
   `tesla` hedder de `sensor.cjp_tesla_overview`. Brug ét prefix pr. bil.
2. **Lader:** status, effekt, kWh-målere, tænd/sluk, kabel og CO₂, samt spænding, faser,
   maks. ampere og ladetab
3. **Lastfordeling:** prioritet og fasefølge (kun med en [fælles el-tilslutning](#flere-biler-lastfordeling))
4. **Elbil:** batteriniveau, rækkevidde, kilometertæller, ladeport/-kabel, ladegrænse,
   position, opdateringstid og klima, samt batteristørrelse. Spring siden over, hvis du ikke har
   en bil-integration (virtuel elbil).
5. **Hus og husbatteri:** forbrug, ignorerede forbrugere, husbatteriets niveau og effekt
6. **Prisudbydere:** Energi Data Service og/eller Strømligning, refusion og salgsgebyr
7. **Solceller, solcelleopladning og vejr** (kun med solceller)
8. **Notifikationer, avanceret** (interval, dashboard, diagnostisk fil-log, testtilstand) **og database**

Entitetsvælgerne viser kun relevante entiteter: batteriniveau kun `battery`-sensorer, effekt
kun `power`-sensorer, kWh kun `energy`-sensorer, priserne kun entiteter fra prisintegrationerne
og prognosen kun Solcast.

Alt kan ændres bagefter under **Konfigurer** på integrationen, via en menu.

---

## Sådan styrer du laderen og bilen

| Entitet | Indhold |
|---|---|
| `sensor.cjp_<prefix>_charger_action` | `charge` eller `stop`: om laderen skal lade nu |
| ↳ attribut `charger_enabled` | om laderen skal være tændt |
| ↳ attribut `circuit_phase_1_amps` … `_3_amps` | kredsløbets grænse pr. fase (f.eks. Easee kredsløbsgrænse) |
| ↳ attribut `charging_limit` | samme ladegrænse som `sensor.cjp_<prefix>_charging_limit` |
| ↳ attributter `phase_1_amps`, `phase_2_amps`, `phase_3_amps` | samme ladestrøm pr. fase som de tre ampere-sensorer (så en automation kun skal bruge `charger_action`) |
| ↳ attribut `mode` | `normal`, `deactivated` (slået fra) eller `error` (beregningen fejlede) |
| ↳ attribut `updated` | tidspunkt for seneste beregning (hjerteslag) |
| `sensor.cjp_<prefix>_charger_phase_1_amps` … `_3_amps` | ønsket ladestrøm pr. fase i A (0 = fasen bruges ikke) |
| `sensor.cjp_<prefix>_charging_watts` | ønsket ladeeffekt |
| `sensor.cjp_<prefix>_charging_limit` | ønsket ladegrænse på bilen i % (kun med bil-integration) |
| `event.cjp_<prefix>_ev_request` | `wake_up`, `preheat_start` (data: `departure`, `defrost`) og `preheat_stop` |

### Blueprint: styr laderen

Blueprinten installeres automatisk sammen med integrationen: ved opstart lægges den i
`config/blueprints/automation/cable_juice_planner/charger_control.yaml` og findes derefter under
*Indstillinger → Automatiseringer og scener → Blueprints* som **Cable Juice Planner - styr
laderen**. Kommer der en ny version med en HACS-opdatering, erstattes filen kun, hvis du ikke
selv har rettet i den.

[`charger_control.yaml`](custom_components/cable_juice_planner/blueprints/charger_control.yaml) klarer
start/stop, tænd/sluk og strøm pr. fase. Du angiver dine egne handlinger, og i dem kan du bruge
variablerne `phase_1`, `phase_2`, `phase_3`, `max_phase`, `phases`, `charger_enabled`,
`fail_safe` og `message`.

Lav én automation pr. bil med blueprinten. Under *Cable Juice Planner* vælger du kun bilens
**Lader handling** (`sensor.cjp_<prefix>_charger_action` – vælgeren viser kun dem); ladestrømmen
pr. fase læses fra dens attributter.

Blueprinten har også fail-safe for det, integrationen ikke selv kan dække. Bliver
`charger_action` utilgængelig, eller er `updated` for gammel, lader den med en fast strøm og
sender en notifikation.

Under **Ladestatus** angiver du, hvordan blueprinten ser om der lades: en status-sensor
(laderens eller bilens, f.eks. `charging`/`completed`), en kontakt eller binær sensor der er
`on` mens der lades, og/eller en effektsensor med en tærskel (standard 500 W). Så gentages start
ikke ved fail-safe og efter en genstart af Home Assistant, mens der lades eller når der meldes
færdig (*Tilstande der betyder færdig*, standard `completed` og `finished` som scriptets
`CHARGER_COMPLETED_STATUS`). En ny ladebeslutning fra integrationen sender altid start.

Under **Genstart når der ikke lades** kan du tilføje handlinger, der køres når handlingen har
stået på `charge` i f.eks. 10 minutter, uden at der lades – typisk
`homeassistant.reload_config_entry` med en af laderens eller bilens entiteter som mål, som
genindlæser den integration. Det gentages efter yderligere 10 og 20 minutter (højst 3 gange som
standard), du får en notifikation, og 1 minut efter sendes ladestrøm og start igen. Det kræver
Ladestatus – uden den kan blueprinten ikke se, om der lades.

Blueprinten har en sektion til **laderen** og en til **bilen**. Alle handlinger er valgfrie: for
hver opgave udfylder du den dér, hvor dit setup kan gøre det – i laderen, i bilen eller begge
steder (f.eks. ladestrøm i laderen og ladegrænse i bilen).
Ved start: ladegrænse i bilen → tænd laderen → ladestrøm i laderen → ladestrøm i bilen →
start laderen → start bilen. Ved stop stoppes begge.

**Typiske setups**

| Setup | Udfyld | Eksempel |
|---|---|---|
| Laderen kan styres, bilen har ingen integration (eller kan ikke styres) | Lader | Easee, Zaptec, Monta, Wallbox eller en OCPP-lader med en "virtuel" bil (batteriniveau sættes manuelt) |
| Bilen kan styres, laderen har ingen integration ("dum" lader, evt. målt med en energimåler) | Bil | VW/Cupra/Skoda, Kia/Hyundai, Tesla … på en lader uden integration – f.eks. målt med en Shelly 3EM |
| Både laderen og bilen har en integration | Hver opgave dér hvor den kan | F.eks. strøm og start/stop i laderen og ladegrænse i bilen – eller start/stop og ladegrænse i bilen og strøm i laderen |

**Typiske handlinger** – navnene afhænger af integrationen; find dem under *Udviklerværktøjer →
Handlinger* og *Tilstande* for din lader og bil:

| Input | Laderens integration | Bilens integration |
|---|---|---|
| Start / stop | en start/stop- eller pause/genoptag-service (f.eks. Easee `action_command`), en start/stop-kontakt (f.eks. Monta, Wallbox, OCPP *Charge Control*) eller en knap | en ladekontakt (f.eks. Tesla/Tessie) eller en start/stop-service (f.eks. Kia/Hyundai Connect `start_charge`/`stop_charge`, VW/Cupra-integrationerne) |
| Ladestrøm | en dynamisk grænse-service (f.eks. Easee, Zaptec) eller en number for maks. strøm (f.eks. OCPP *Maximum Current*, Wallbox *Max charging current*) – `{{ max_phase }}` eller pr. fase `{{ phase_1 }}` … | kun nogle biler har en number for ladestrømmen (f.eks. Tesla/Tessie *charge current*) – `{{ max_phase }}` |
| Ladegrænse | – | en number for ladegrænsen (f.eks. *charge limit*, *AC charging limit*, *target state of charge*) – `{{ charging_limit }}` |
| Tænd / sluk | laderens "aktiveret"-kontakt – vælg entiteten, ikke enheden | – |
| Ladestatus | laderens status-sensor (tilpas *lader*/*færdig*-tilstandene, f.eks. `charging`, `completed`, `finished`, OCPP `Charging`/`Finishing`) eller en effektsensor / energimåler | bilens "lader"-kontakt eller binære sensor, eller bilens ladestatus |
| Genstart | `homeassistant.reload_config_entry` med en af laderens entiteter som mål | `homeassistant.reload_config_entry` med en af bilens entiteter som mål |

Kan hverken laderen eller bilen sætte ladestrømmen, starter og stopper blueprinten bare
ladningen – soloverskud og lastfordeling kan så ikke regulere strømmen.

Brug blueprintens variabler (`{{ phase_1 }}`, `{{ max_phase }}`, `{{ charging_limit }}` …)
frem for at læse sensorerne selv – i fail-safe har variablerne fail-safe-strømmen.

### Eksempel: egne automationer uden blueprinten

```yaml
alias: CJP - ladegrænse
triggers:
  - trigger: state
    entity_id: sensor.cjp_charging_limit
conditions:
  - "{{ trigger.to_state.state | is_number }}"
actions:
  - action: number.set_value
    target:
      entity_id: number.min_bil_charge_limit
    data:
      value: "{{ trigger.to_state.state | int }}"
---
alias: CJP - væk og forvarm bilen
triggers:
  - trigger: state
    entity_id: event.cjp_ev_request
actions:
  - choose:
      - conditions: "{{ trigger.to_state.attributes.event_type == 'wake_up' }}"
        sequence:
          - action: button.press
            target:
              entity_id: button.min_bil_wake
      - conditions: "{{ trigger.to_state.attributes.event_type == 'preheat_start' }}"
        sequence:
          - action: climate.turn_on
            target:
              entity_id: climate.min_bil_climate
      - conditions: "{{ trigger.to_state.attributes.event_type == 'preheat_stop' }}"
        sequence:
          - action: climate.turn_off
            target:
              entity_id: climate.min_bil_climate
```

### Fail-safe

- **Beregningen fejler:** `mode: error`, og handlingen bliver `charge` med maks. strøm og det
  anbefalede maksimum som ladegrænse. Efter 3 fejl i træk genindlæses integrationen.
- **Integrationen er slået fra** (*Deaktiver scriptet*): `mode: deactivated`, og handlingen
  bliver `charge` med maks. strøm.
- **Laderen lader ikke, selvom den skal:** `charger_enabled` slås fra og til (op til 3 gange)
  med notifikationer undervejs.
- **Priser eller Solcast mangler:** der planlægges ud fra integrationens egne databaser.
- **Integrationen er stoppet eller hænger:** det dækker [blueprinten](#blueprint-styr-laderen).

---

## Flere biler (lastfordeling)

Lader flere biler på samme el-tilslutning, kan de tilsammen overstige hovedsikringen. Tilføj
derfor én **Fælles el-tilslutning** (**Tilføj integration → Cable Juice Planner → Fælles el-tilslutning**):

| Felt | Betydning |
|---|---|
| Hovedsikring L1/L2/L3 | grænsen pr. fase (eller den del bilerne tilsammen må bruge) |
| Sikkerhedsmargin | trækkes fra sikringen (standard 1 A) |
| Strøm/effekt L1/L2/L3 (valgfri) | husets målte strøm (A) eller effekt (W) pr. fase, f.eks. fra en HAN/P1-måler |
| Netspænding | omregner W til A |
| Tur snart | en tur inden for så mange timer giver bilen forrang (standard 12) |

Under hver bil vælger du **prioritet** (1 = først) og **fasefølge** (L1-L2-L3, L2-L3-L1 eller
L3-L1-L2), så 1-fase-ladning belaster den rigtige fase.

- **Rækkefølge:** bilerne ordnes efter prioritet. En bil under dagligt minimum, eller med en tur
  inden for *Tur snart*, går forrest.
- **Planlægning:** hver bil får en kapacitet pr. time. Den er sikringen minus margin, minus husets
  forventede forbrug (gennemsnit af samme time den sidste uge, kun med fase-sensorer), minus det
  bilerne foran har reserveret. Er der ikke plads til fuld strøm, lader bilen med færre ampere,
  og resten planlægges i andre billige timer.
- **1 eller 3 faser:** den største effekt, der passer inden for den ledige strøm pr. fase, vælges.
  Bilen kan altså skifte til 1 fase, når en fase er fyldt.
- **Live:** ampere begrænses altid til det, der er ledigt lige nu. Ændrer en bil foran sin plan eller
  sit strømforbrug, genberegnes bilerne bagved efter 15 sekunder. Med fase-sensorer begrænses bilen
  med lavest prioritet med det samme, hvis en fase går over sikringen.

| Sensor | Indhold |
|---|---|
| `sensor.cjp_site_available_l1` … `_l3` | ledig strøm pr. fase (attributten `cars` viser rækkefølge og hver bils ønske) |
| `sensor.cjp_site_house_l1` … `_l3` | husets strøm pr. fase, uden bilerne |

---

## Entiteter

Indstillingerne fra dashboardet er entiteter, som huskes ved genstart:

| Type | Eksempler |
|---|---|
| `switch` | laderegler, arbejdsdage og forvarmning pr. dag, manuel ladning, tvangsladning, beregn ladetab, deaktiver |
| `number` | km pr. arbejdsdag, batteriniveauer, ladeprocent ved billig strøm, tur, fuld ladning hver X dage, pris for offentlig ladning, virtuel elbil |
| `time` | afgang og hjemkomst pr. arbejdsdag |
| `datetime` | turens afgang og hjemkomst, sidste fulde opladning |
| `select` | prognosetype |
| `button` | gennemtving planlægning, nulstil tur, genindlæs integrationen |

Klikker du på en indstilling, står der en kort forklaring under **Attributter** (`description`
og `help`). Planen beregnes igen 5 sekunder efter en ændring.

Resultaterne er bl.a. `sensor.cjp_current_charging_rule`, `_overview`, `_charging_history`,
`_emoji_description`, `_kwh_cost_price`, `_drive_efficiency`, `_km_per_kwh`, `_estimated_range`,
`_co2_emitted` og `_kwh_charged_by_solar`. Store attributter (markdown, planer) gemmes ikke i
recorder-databasen.

### Offentlig opladning

Med bil-integration registreres en ladning væk fra hjemmet automatisk: tid, batteriniveau ved
start og slut, og tilføjede procent.

1. Indtast den samlede pris i `number.cjp_public_charging_session_cost`.
   Indtaster du `-1`, slettes ladningen.
2. Efter ca. 1 minut lægges ladningen i historikken, og feltet nulstilles.
3. Har du flere ladninger, så indtast dem i den rækkefølge, de er sket.

Ladninger kan også tilføjes med servicen `cable_juice_planner.manual_add_public_charging_session`.

---

## Dashboard

Med **Dashboard i sidepanelet** slået til (standard) får hver bil sit eget dashboard,
f.eks. `/cjp-planner` (uden prefix) eller `/cjp-tesla`. Det bygges ved hver opstart ud fra
din opsætning. Kort for ting, du ikke har sat op, bliver fjernet.

Det genererede dashboard kan ikke redigeres i brugerfladen. Vil du tilpasse det, så kopiér
indholdet over i et almindeligt dashboard (**Rediger → Raw configuration editor**), og slå
**Dashboard i sidepanelet** fra.

---

## Services

| Service | Beskrivelse |
|---|---|
| `cable_juice_planner.enforce_planning` | Beregn planen igen nu |
| `cable_juice_planner.manual_add_public_charging_session` | Beregn (og evt. gem) en offentlig ladning |
| `cable_juice_planner.debug_info` | Returnerer intern status til fejlfinding |

Alle services tager valgfrit `config_entry_id`, så de kun rammer én bil.

---

## Skift fra PyScript-udgaven

> [!WARNING]
> Lad PyScript-udgaven køre, mens integrationen tilføjes. Ellers er dens hjælpere og sensorer
> væk, og deres værdier kan ikke kopieres.

1. Tilføj integrationen. Findes der PyScript-filer (`ev_config.yaml`, `ev2_config.yaml`, …),
   spørger opsætningen hvilken installation der skal importeres:
   - værdierne fra `<navn>_config.yaml` udfyldes på siderne
   - `<navn>_*_db.yaml` (ladehistorik, effektværdier, solproduktion, kWh-priser,
     køreeffektivitet og km/kWh) importeres ved første start
   - værdierne fra `input_boolean/input_number/input_select/input_datetime.<navn>_*` og
     template-sensorerne kopieres til de nye entiteter
2. Lav automationerne på [handlings-entiteterne](#sådan-styrer-du-laderen-og-bilen), men lad dem
   være slået fra, så længe PyScript-udgaven styrer laderen. De nye entiteter starter med `cjp_`
   og støder ikke sammen med PyScript-udgavens `ev_*`.
3. Sammenlign planerne et par dage.
4. Fjern `pyscript/ev.py` og `packages/ev.yaml`, genstart, og slå automationerne til.

PyScript-filerne bliver kun læst, aldrig ændret. Integrationen gemmer sine egne data i `.storage`.

| PyScript | Integration |
|---|---|
| `ev_config.yaml` | Opsætning / **Konfigurer** i brugerfladen |
| `input_boolean.ev_*` | `switch.cjp_*` |
| `input_number.ev_*` | `number.cjp_*` |
| `input_datetime.ev_workday_*` | `time.cjp_*` |
| `input_datetime.ev_trip_*`, `_last_full_charge` | `datetime.cjp_*` |
| `input_button.ev_*` | `button.cjp_*` |
| Ladekommandoer (`charge_switch`, `charging_amps`, `wake_up`, dynamiske grænser, …) | [Handlings-entiteter](#sådan-styrer-du-laderen-og-bilen) + automation |
| Andre biler på laderen | [Fælles el-tilslutning](#flere-biler-lastfordeling) |
| `pyscript.ev_manual_add_public_charging_session` | `cable_juice_planner.manual_add_public_charging_session` |
| Opdateringsscripts og `shell_command` | HACS |

### Ændringer i forhold til PyScript-udgaven

- Ingen kommandoer til lader eller bil. Se [handlings-entiteterne](#sådan-styrer-du-laderen-og-bilen).
- Lastfordeling mellem flere biler med prioritet og hovedsikring.
- Forbrugshistorik gemmes også uden solceller og Solcast.
- Offentlige ladninger huskes efter en genstart.
- Den samme notifikation sendes højst én gang i timen.
- Historikken læses fra en buffer i hukommelsen i stedet for 70+ recorder-opslag pr. planlægning.

---

## Fejlfinding

- Slå **Diagnostisk fil-log** til under **Konfigurer → Avanceret**. Loggen skrives til
  `cable_juice_planner_<prefix>.log` i konfigurationsmappen.
- Eller slå debug-log til i `configuration.yaml`:
  ```yaml
  logger:
    logs:
      custom_components.cable_juice_planner: debug
  ```
- Kald `cable_juice_planner.debug_info` under **Udviklerværktøjer → Handlinger**, og vedhæft svaret, når du opretter et issue.

---

## Licens

GPL-3.0, se [LICENSE](LICENSE).
