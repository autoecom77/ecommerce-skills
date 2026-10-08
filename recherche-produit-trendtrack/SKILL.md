---
name: recherche-produit-trendtrack
description: "Runs the TrendTrack product research: ads → COGS → Sheet."
version: 2.2.0
---

# Recherche Produit TrendTrack

Workflow automatisé de recherche produit dropshipping EU. Tourne en cron tous les 2 jours à 9h00 Paris (7h00 UTC). Ajoute au Google Sheet « Tableau de Recherche Produit » tous les produits qui passent les filtres, classés par potentiel et par niche. Trigger : cron tous-les-2-jours, « recherche produit », « trouve des produits ».

## Accès

- **TrendTrack REST API** : clé `sk_tt_...` dans `~/.hermes/mcp-tokens/trendtrack_rest_key.txt`.
- **Sheet destination** : « Tableau de Recherche Produit » — ID `1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM`, onglet `Products` (gid 815136391). Colonnes 1 à 73 remplies ; colonnes `Conc1` (BV) et `Conc2` (BW) restent vides (concurrents non enrichis).
- **Google API CLI** :
  ```bash
  GAPI="$HOME/.hermes/venvs/gws/bin/python $HOME/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
  ```
  `sheets get` (lecture), `sheets update` (écriture à position exacte), `gmail send` (récap).
- **Scripts helper** (dans `~/.hermes/skills/ecommerce/recherche-produit-trendtrack/scripts/`) :
  - `query_ads_api.py` — batch des 2 méthodes de recherche TrendTrack sur 5 pages via l'API REST
  - `pre_filter.py` — déduplication domaine contre les boutiques déjà présentes dans le Sheet
  - `price_filter.py` — filtrage mathématique déterministe (prix < 30€, near-miss 29.90–29.99€, coefficient X > 4 si 30–40€, X ≥ 3.5 si > 40€)
  - `cogs_image_search.py` — recherche COGS par image (Apify 1688 + fallback AliExpress Scrapling)
  - `aliexpress_image_search.py` — moteur de recherche par image AliExpress
  - `enrich_api.py` — récupération programmatique des top ads actives et génération des URLs TrendTrack

## Étape 0 — Pré-checks

1. Date du jour : `date +%F` (notée J, sert aux filtres de dates).
2. Vérification crédits : lire le solde TrendTrack (`GET https://api.trendtrack.io/v1/usage`). Si remaining < 300 : STOP et email d'alerte « crédits TrendTrack faibles (N restants) » à autoecom77@gmail.com.
3. Extraction des URLs existantes pour dédoublonnage :
   ```bash
   $GAPI sheets get 1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM 'Products!B2:C2000' \
     > /tmp/tt_sheet_existing.json
   ```

## Étape 1 — Recherche batch (`query_ads_api.py`)

Lancer le script qui exécute les 2 méthodes de recherche (Shopify reach growth, volume d'ads live) sur 5 pages via l'API REST publique, fusionne et déduplique par domaine :

```bash
cd ~/.hermes/skills/ecommerce/recherche-produit-trendtrack
python3 scripts/query_ads_api.py --date $(date +%F) > /tmp/tt_candidates_raw.json
# Mode test 1 méthode : --method 1|2 (≈ 30 crédits/page, ex. --method 1 --pages 1)
# Personnaliser le nombre de pages : --pages <N> (défaut : 5)
```

Le script interroge par défaut 5 pages par méthode (100 ads/page). Coût recherche : 300 crédits fixes (2 méthodes × 5 pages × 30 crédits).

## Étape 1.5 — Déduplication Sheet (`pre_filter.py`)

Éliminer immédiatement les boutiques déjà enregistrées dans le Google Sheet :

```bash
python3 scripts/pre_filter.py \
  --candidates /tmp/tt_candidates_raw.json \
  --sheet-data /tmp/tt_sheet_existing.json \
  > /tmp/tt_candidates_filtered.json
```

## Étape 2 — Prix de vente & Pré-filtrage prix

Pour chaque candidat du JSON filtré, extraire le prix de vente en euros depuis sa landing page (`landing_url`).
Puis lancer le pré-filtrage prix pour éliminer immédiatement les produits vendus à moins de 30 € avant la recherche COGS :

```bash
python3 scripts/price_filter.py --mode price-only \
  --input /tmp/tt_candidates_with_prices.json \
  --near-misses-file /tmp/tt_near_misses.json \
  > /tmp/tt_candidates_priced.json
```

Le script élimine les produits < 30 € et enregistre les quasi-miss (29,90–29,99 €) dans `/tmp/tt_near_misses.json` pour citation dans l'email.
Seuls les produits validés passent à la recherche COGS (évite de consommer du temps/requêtes sur des produits non éligibles).

## Étape 3 — Recherche COGS par image & Validation ratio

COGS = prix produit **seul** (hors frais de port). Conversion ¥→€ ≈ 0,128 (¥30 ≈ 3,84 €). Toujours consigner la source dans le Commentaire (« COGS 1688 ¥XX » ou « COGS AliExpress XX€ »).

Pour chaque candidat retenu, lancer la recherche COGS par image à partir du visuel de la pub (`image_url`) :

```bash
python3 scripts/cogs_image_search.py \
  --image-url "<image_url du candidat>" \
  --product-name "<nom EN du produit>"
```

Ordre de recherche du script :
1. **1688 via Apify Image Search** (si configuré)
2. **AliExpress Image Search** via `aliexpress_image_search.py` (Scrapling)
3. Si introuvable des deux $\rightarrow$ calcul heuristique : COGS = Prix ÷ diviseur de niche (Cosmétique 6.5, Maison/Cuisine 5, Apparel mécanisme 6, Outdoor/Hobby/Sport 5, Santé soft 6.5, Puériculture 6, défaut 5.5) avec mention « COGS estimé (heuristique) » en Commentaire.

### Validation mathématique finale du coefficient ($X$)
Une fois tous les COGS renseignés, appliquer le filtrage strict déterministe :

```bash
python3 scripts/price_filter.py --mode full \
  --input /tmp/tt_candidates_with_cogs.json \
  --near-misses-file /tmp/tt_near_misses.json \
  > /tmp/tt_candidates_kept.json
```

Règles appliquées par le script :
- Prix < 30 € $\rightarrow$ exclu
- Prix 30–40 € $\rightarrow$ $X > 4{,}0$ requis (strictement supérieur à 4)
- Prix > 40 € $\rightarrow$ $X \ge 3{,}5$ requis
- Les produits sous le seuil sont exclus (loggués sur stderr).

La sortie `/tmp/tt_candidates_kept.json` contient uniquement les produits validés avec leur `x_coefficient` exact.

## Étape 4 — Score /100

| Critère | Points |
|---|---|
| X ≥ 5 | +25 |
| X 4–5 | +20 |
| X 3.5–4 | +12 |
| Croissance reach 30j ≥ 300% | +25 |
| Croissance 135–300% | +15 |
| Ads actives ≥ 100 | +20 |
| Ads actives 50–100 | +12 |
| Ads actives 20–50 | +8 |
| Niche prioritaire (cosmétique, maison/cuisine, apparel mécanisme, sport/outdoor, santé soft) | +15 |
| Days running ≥ 20 | +10 |
| Focus géo ≥ 50% sur un pays | +10 |

Cap à 100. **Priorité** : Score ≥ 70 → `1 Urgent` ; 50–69 → `2 Important` ; < 50 → `3 Normal`.

## Étape 5 — Enrichissement par produit (`enrich_api.py`)

Pour chaque produit retenu, récupérer ses pubs actives via l'API REST TrendTrack :

```bash
cd ~/.hermes/skills/ecommerce/recherche-produit-trendtrack
python3 scripts/enrich_api.py \
  --candidates /tmp/tt_candidates_kept.json \
  > /tmp/tt_enriched.json
# Test unitaire : python3 scripts/enrich_api.py --shop <domaine>
```

Le script retourne pour chaque boutique :
- `ads_count` : nombre d'ads actives (à reporter dans la colonne « Ads » / F, plafonné à 20).
- `ads[]` triées par reach décroissant : génère les liens TrendTrack `https://app.trendtrack.io/ads/<id>` pour remplir les blocs AD1 à AD8.
- Les colonnes concurrents (BV/BW) restent vides.

## Étape 6 — Écriture dans le Sheet

Ligne de 75 colonnes (JAMAIS modifier les lignes existantes) :
```
[FALSE, Nom, URL, Pays, Niche, Ads, Priorité, "", COGS, Prix, X,
 AD1_url, AD1_imp, AD1_date1, AD1_date2, … AD8 max (blocs de 4),
 …, Commentaire, Score, "", ""]
```
- Colonnes 12–71 : blocs AD1..AD15 (url, impressions/spend, date 1, date 2) — remplir 8 ads max, le reste vide.
- Dates format `Mmm YY` (ex. `'Dec 25`) **en texte forcé** préfixé d'une apostrophe pour éviter les réinterprétations par Google Sheets.
- Impressions/spend format bucket : `10 M +`, `7 - 10 M`, `3 - 7 M`, `1 - 3 M`, `< 1 M`.
- URL ad : `trendtrack_url` fourni par `enrich_api.py`.
- Lignes classées par Priorité croissante puis par niche.
- Écriture atomique à la première ligne vide :
  ```bash
  $GAPI sheets update 1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM "Products!A<L+1>:BW<L+n>" --values '<JSON>'
  ```

## Étape 7 — Rapport par email

```bash
$GAPI gmail send --to autoecom77@gmail.com --subject "Recherche produit TrendTrack — <date>" --body "<résumé>"
```
Résumé à inclure :
- Nombre de produits examinés / retenus / ajoutés.
- Crédits consommés (recherche 300 + ~5 crédits par boutique enrichie).
- Produits quasi-miss (29,90–29,99 €) capturés depuis `/tmp/tt_near_misses.json`.
- Top 3 des produits ajoutés (nom, prix, X, score, priorité).
- Lien direct vers le Sheet : https://docs.google.com/spreadsheets/d/1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM/edit?gid=815136391

## Règles opérationnelles

- Crédits : cible moyenne ≤ 650 crédits/run, plafond dur 1 200. Coûts : 300 crédits (recherche initiale : 2 méthodes × 5 pages × 30 crédits) + ~5 crédits par boutique enrichie. Si le solde restant est < 300 crédits au départ, annuler le run et envoyer un email d'alerte.
- Colonnes `Validation / Lancement` et `Kalodata` restent vides (remplissage manuel ultérieur).
- Colonnes `Concurrent 1` et `Concurrent 2` restent vides.
