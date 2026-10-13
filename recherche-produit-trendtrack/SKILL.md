---
name: recherche-produit-trendtrack
description: "Runs the TrendTrack product research: ads → COGS → Sheet."
version: 2.4.0
---

# Recherche Produit TrendTrack

Workflow automatisé de recherche produit dropshipping EU. Tourne en cron tous les 3 jours à 9h00 Paris (7h00 UTC, `0 7 */3 * *`). Ajoute au Google Sheet « Tableau de Recherche Produit » tous les produits qui passent les filtres de prix et de coefficient de marge $X$, classés par rentabilité ($X$ décroissant).

## Exécution en 1 Commande (Mode Express / Cron)

Pour lancer le pipeline complet de bout en bout de façon 100% autonome et déterministe :

```bash
cd ~/.hermes/skills/ecommerce/recherche-produit-trendtrack
python3 scripts/run_pipeline.py
# Options : --pages <N> (défaut 5), --dry-run (simulation sans écriture), --date YYYY-MM-DD,
#           --credit-cap 1200, --search-budget 850, --cogs-workers 4,
#           --resume-from search|prices|cogs (reprise après crash/arrêt SANS relancer la recherche payée)
```

stdout = résumé JSON final (entonnoir, crédits, top 3, quasi-misses), aussi écrit dans `/tmp/tt_run/run_summary.json` ; logs sur stderr. Le run complet prend ~15–30 min (COGS) : en cron, le lancer avec `terminal(background=true)` puis `process(wait)` en boucle.

---

## Accès & Outils

- **TrendTrack REST API** : clé `sk_tt_...` dans `~/.hermes/mcp-tokens/trendtrack_rest_key.txt`.
- **Apify 1688 Token** : clé dans `~/.hermes/apify_token` (acteur `crawleast/1688-image-search-scraper`).
- **Sheet destination** : « Tableau de Recherche Produit » — ID `1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM`, onglet `Products` (gid 815136391).
- **Google API CLI** :
  ```bash
  GAPI="$HOME/.hermes/venvs/gws/bin/python $HOME/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
  ```
- **Scripts de la Skill** (dans `~/.hermes/skills/ecommerce/recherche-produit-trendtrack/scripts/`) :
  - `run_pipeline.py` — Orchestrateur master exécutant toutes les étapes de bout en bout
  - `query_ads_api.py` — Batch des 2 méthodes de recherche TrendTrack sur 5 pages via l'API REST
  - `pre_filter.py` — Déduplication exacte par domaine contre les boutiques déjà présentes dans le Sheet
  - `extract_prices.py` — Extraction multithreadée des prix de vente web (Shopify `.js`, JSON-LD, regex)
  - `price_filter.py` — Filtrage mathématique déterministe (prix < 30€, near-miss 29.90–29.99€, coefficient $X > 4$ si 30–40€, $X \ge 3.5$ si > 40€)
  - `cogs_image_search.py` — Recherche COGS en cascade 3 niveaux (Apify 1688 $\rightarrow$ AliExpress navigateur $\rightarrow$ 1688 mot-clé $\rightarrow$ heuristique)
  - `aliexpress_image_search.py` — Moteur de recherche d'image AliExpress via navigateur furtif Scrapling
  - `enrich_api.py` — Récupération des 8 top ads actives TrendTrack au format `ju-2`
  - `assemble_rows.py` — Assemblage des lignes 75 colonnes sans score, triées par $X$ décroissant
  - `write_sheet.py` — Écriture atomique sécurisée à la première ligne vide du Google Sheet
  - `send_recap.py` — Compilation des statistiques d'entonnoir et envoi du récapitulatif par email

---

## Détail des Étapes du Pipeline

### Étape 0 — Pré-checks
1. Vérification solde crédits TrendTrack (`GET https://api.trendtrack.io/v1/usage`). Si remaining < 300 : arrêt immédiat.
2. Extraction des URLs existantes pour dédoublonnage :
   ```bash
   $GAPI sheets get 1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM 'Products!B2:C2000' > /tmp/tt_sheet_existing.json
   ```

### Étape 1 — Recherche batch (`query_ads_api.py`)
Interroge les 2 méthodes de recherche TrendTrack (Shopify reach growth, volume d'ads live) jusqu'à 5 pages chacune (100 ads/page, reach max 10M), **en round-robin (M1 p1, M2 p1, M1 p2…) et sous budget crédits** (`--max-credits`, coût lu dans l'en-tête `X-Usage-Cost`) :
```bash
python3 scripts/query_ads_api.py --date $(date +%F) --pages 5 --max-credits 850 > /tmp/tt_candidates_raw.json
```
Coût réel mesuré : **~1,5 crédit par ad renvoyée = ~150 crédits par page de 100** (et non 30/appel). 5 pages × 2 méthodes coûteraient ~1 500 crédits > plafond : avec le budget par défaut de 850, on obtient ~M1 3 pages + M2 2 pages (750 crédits), le reste du plafond allant à l'enrichissement. Stats par méthode → `/tmp/tt_query_stats.json`.

### Étape 1.5 — Déduplication Sheet (`pre_filter.py`)
Élimine les boutiques déjà enregistrées dans le Google Sheet par domaine :
```bash
python3 scripts/pre_filter.py --candidates /tmp/tt_candidates_raw.json --sheet-data /tmp/tt_sheet_existing.json > /tmp/tt_candidates_filtered.json
```

### Étape 2 — Prix de vente & Pré-filtrage prix
1. Extraction multithreadée (6 threads) du prix de vente (EUR) depuis les landing pages. Les boutiques Shopify répondent **429/430 par boutique et par IP** après une rafale : retry avec backoff, puis erreur explicite `HTTP 429 rate limited`. **Ne jamais relancer l'extraction sur les mêmes ~350 boutiques dans l'heure** (2e passe = 40/355 prix au lieu de 98/355) — reprendre avec `--resume-from cogs`. Un `"price":4500` entier ≥ 100 dans le JSON inline d'une page Shopify = **centimes** (45,00 €) ; un entier < 100 reste en euros.
   ```bash
   python3 scripts/extract_prices.py --input /tmp/tt_candidates_filtered.json --output /tmp/tt_candidates_with_prices.json
   ```
2. Pré-filtrage prix < 30 € & capture des quasi-misses (29,90–29,99 €) :
   ```bash
   python3 scripts/price_filter.py --mode price-only \
     --input /tmp/tt_candidates_with_prices.json \
     --near-misses-file /tmp/tt_near_misses.json \
     > /tmp/tt_candidates_priced.json
   ```

### Étape 3 — Recherche COGS en Cascade 3 Niveaux & Ratio $X$
Pour chaque candidat avec prix (≥ 30 € ; les candidats sans prix sont ignorés), 4 recherches en parallèle (Apify gratuit = 5 runs simultanés max), timeout 420 s par candidat (process group tué au timeout). Le COGS est recherché selon l'ordre strict :
1. **Niveau 1** : 1688 via Apify Image Search (`crawleast/1688-image-search-scraper`).
2. **Niveau 2** : AliExpress Reverse Image Search via navigateur (`aliexpress_image_search.py`).
3. **Niveau 3** : 1688 Recherche textuelle par mots-clés (`crawleast/1688-product-scraper`).
4. **Niveau 4 (Fallback)** : Calcul heuristique : $COGS = Prix \div Diviseur$ (Cosmétique 6.5, Maison/Cuisine 5, Apparel 6, Sport/Outdoor 5, Santé 6.5, Puériculture 6, défaut 5.5).

Chaque niveau prend la **médiane** des matches (pas le moins cher : le moins cher est souvent une pièce/échantillon à ¥1 — ex. 0,11 € pour un vélo à 1 999 €) et est **rejeté si Prix/COGS > 30** (max historique du Sheet : 28,3) → niveau suivant. `cogs_image_search.py --price-eur <prix>` active ce contrôle ; la raison des rejets est conservée (`cogs_attempts`) et visible dans la note COGS.

Validation mathématique finale du coefficient ($X = Prix \div COGS$) :
```bash
python3 scripts/price_filter.py --mode full \
  --input /tmp/tt_candidates_with_cogs.json \
  --near-misses-file /tmp/tt_near_misses.json \
  > /tmp/tt_candidates_kept.json
```
Règles strictes :
- Prix < 30 € $\rightarrow$ exclu
- Prix 30–40 € $\rightarrow$ $X > 4{,}0$ requis
- Prix > 40 € $\rightarrow$ $X \ge 3{,}5$ requis

### Étape 4 — Enrichissement TrendTrack (`enrich_api.py`)
Récupère les 8 top ads (`--limit 8`, facturé par ad renvoyée, ~12 crédits max/boutique) de chaque boutique retenue au format explorer workspace `ju-2`, dans la limite du budget restant (`--max-credits` = plafond − crédits déjà consommés) ; au-delà, seule l'ad source est listée :
```bash
python3 scripts/enrich_api.py --candidates /tmp/tt_candidates_kept.json > /tmp/tt_enriched.json
```

### Étape 5 — Formatage des Lignes (`assemble_rows.py`)
Construit la structure exacte des 75 colonnes (A à BW) :
```bash
python3 scripts/assemble_rows.py \
  --candidates /tmp/tt_candidates_kept.json \
  --enriched /tmp/tt_enriched.json \
  --output /tmp/tt_run/rows.json \
  --products-out /tmp/tt_run/final_products.json
```
- **Tri** : Tri direct par coefficient $X$ décroissant (meilleure marge d'abord).
- **Colonnes Score /100 (BU) et Priorité (G)** : laissées vides (`""`).
- **Dates** : format `'Mmm YY` avec apostrophe textuelle forcée.
- **Buckets de reach** : `10 M +`, `7 - 10 M`, `3 - 7 M`, `1 - 3 M`, `< 1 M`.

### Étape 6 — Écriture dans le Google Sheet (`write_sheet.py`)
Détecte dynamiquement la première ligne vide en colonne B et écrit atomiquement :
```bash
python3 scripts/write_sheet.py --rows /tmp/tt_run/rows.json --result-out /tmp/tt_run/sheet_write_result.json
```

### Étape 7 — Rapport Email (`send_recap.py`)
Compile l'entonnoir complet (recherche par méthode → boutiques uniques → dédup Sheet → prix extraits → prix ≥ 30 € → COGS par source → X validé → ajoutés), les crédits (recherche / enrichissement / solde), les quasi-misses et le top 3, puis envoie l'email récapitulatif (`compute_funnel()` est réutilisé par `run_pipeline.py` pour le résumé JSON) :
```bash
python3 scripts/send_recap.py --send --date $(date +%d/%m/%Y)
```

---

## Règles Opérationnelles

- Crédits TrendTrack : cible moyenne $\le 650$ crédits/run, plafond dur 1 200 (appliqué par `run_pipeline.py` : budget recherche 850 + enrichissement sur le reste). Si solde initial < 300 crédits, stopper le run. Un run par défaut consomme ~1 000–1 150 crédits : pour viser 650, baisser `--search-budget` (~450 = 3 pages).
- Colonnes `Validation / Lancement`, `Kalodata`, `Score /100`, `Concurrent 1`, `Concurrent 2` restent vides.
- Colonne D (Pays) = nom français du pays principal de l'ad (DE → Allemagne…), colonne BT = `M1 reach growth` / `M2 volume live` / `M1+M2` + note COGS.

## Pièges (leçons du run du 13/10/2026)

- `python3` = venv Hermes **3.11** (le Python système est 3.14) : les annotations sont évaluées à la définition, un nom indéfini dans une annotation (`-> Tuple_rows`) plante le script en 3.11 alors qu'un test en 3.14 passe. Tester avec `python3` (3.11).
- Toujours relire `extract_ad_fields()` après modification : un `ad_id` non défini faisait échouer chaque page (exception attrapée → 0 candidat mais crédits facturés).
- Avant un vrai run après modification du code : test à ~3 crédits `query_ads_api.py --method 1 --pages 1 --limit 2 --max-credits 4` + `--dry-run` du pipeline.
- Si le run est interrompu après la recherche, **ne pas relancer de zéro** (re-facturation ~750 crédits) : `run_pipeline.py --resume-from cogs` (ou `prices` si l'extraction des prix doit être refaite — attention aux 429). Les artefacts du run précédent sont archivés dans `/tmp/tt_run/archive_*`.
- M2 (volume_live) remonte beaucoup de grandes marques (EcoFlow, Calvin Klein, La Prairie…) et de pages non-produit : la majorité tombe faute de prix ; celles qui ont un prix passent souvent avec un COGS heuristique (X = 5,5) — à relire dans le Sheet.
- **Apify gratuit = 5 runs simultanés.** Un run qu'on arrête de poller continue (timeouts acteurs 2 400 s / 3 600 s) et bloque un slot → tous les runs suivants prennent **HTTP 402** et la cascade COGS tombe en heuristique en ~9 s par produit. Les runs sont maintenant lancés avec `?timeout=&memory=1024` et abortés quand le polling abandonne. Si des 402 réapparaissent : `GET https://api.apify.com/v2/actor-runs?status=RUNNING` puis `POST /v2/actor-runs/<id>/abort` sur chacun, puis `--resume-from cogs`.
- Une série de COGS « no match » sur les 3 niveaux en < 15 s = panne d'infra (402, AliExpress « image search button » introuvable), pas des produits introuvables : vérifier `cogs_attempts` avant d'écrire dans le Sheet.
