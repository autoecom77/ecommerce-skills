---
name: recherche-produit-trendtrack
description: "Runs the TrendTrack product research: ads → COGS → Sheet."
version: 2.0.0
---

# Recherche Produit TrendTrack

Workflow automatisé de recherche produit dropshipping EU. Tourne en cron tous les 2 jours à 9h00 Paris (7h00 UTC). Ajoute au Google Sheet « Tableau de Recherche Produit » TOUS les produits qui passent les filtres — aucun plafond de nombre, cible ≥ 5 par run — classés par potentiel et par niche. Trigger : cron tous-les-2-jours, « recherche produit », « trouve des produits ».

## Accès (déjà configurés sur cette machine)

- **TrendTrack MCP V3** : serveur `trendtrack` (OAuth) dans `~/.hermes/config.yaml` → outils `mcp__trendtrack__*` (`search_ads`, `scan_ad`, `find_similar_shops`, `check_credits`). S'ils ne sont pas chargés dans la session : `tool_search` puis `tool_describe`.
- **Sheet destination** : « Tableau de Recherche Produit » — ID `1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM`, onglet `Products` (gid 815136391). 75 colonnes : 72 d'origine + `Score /100` (BU) + `Concurrent 1` (BV) + `Concurrent 2` (BW).
- **Google API CLI** :
  ```bash
  GAPI="$HOME/.hermes/venvs/gws/bin/python $HOME/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
  ```
  `sheets get` (lecture), `sheets update` (écriture à position exacte), `gmail send` (récap).
- **Scripts helper** (dans `~/.hermes/skills/ecommerce/recherche-produit-trendtrack/scripts/`) :
  - `query_ads.py` — batch des 3 méthodes de recherche TrendTrack
  - `pre_filter.py` — pré-filtrage programmatique (dédup domaine vs Sheet)
  - `cogs_image_search.py` — recherche COGS par image (Apify 1688 + fallback AliExpress)
  - `aliexpress_image_search.py` — recherche par image AliExpress (Scrapling, utilisé comme fallback par cogs_image_search.py)

## Étape 0 — Pré-checks (obligatoires)

1. Date du jour : `date +%F` (notée J, sert aux filtres de dates).
2. `mcp__trendtrack__check_credits` → si remaining < 500 : STOP et email d'alerte « crédits TrendTrack faibles (N restants) » à autoecom77@gmail.com.
3. Extraction données existantes pour dédoublonnage :
   ```bash
   $GAPI sheets get 1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM 'Products!B2:C2000' \
     > /tmp/tt_sheet_existing.json
   ```
   Ce fichier sera passé au script de pré-filtrage à l'étape 1.5.

## Étape 1 — Recherche : script batch `query_ads.py`

Lancer le script qui exécute les 3 méthodes de recherche en un seul batch (Shopify reach growth, native ads, volume d'ads live) et fusionne/déduplique par domaine :

```bash
cd ~/.hermes/skills/ecommerce/recherche-produit-trendtrack
python3 scripts/query_ads.py --date $(date +%F) > /tmp/tt_candidates_raw.json
```

Le script retourne un JSON array de candidats (~20-50 après dédup des 3×20 résultats). Pas de relance de page 2 : le volume initial est suffisant. Coût : 90 crédits fixes (3 × `search_ads` à 30 crédits). Si une ad clé manque de données (dates, reach), compléter avec `scan_ad(ad_identifier=...)`.

## Étape 1.5 — Pré-filtrage programmatique

Filtrer les candidats avant l'analyse LLM avec le script de pré-filtrage (élimine les domaines déjà présents dans le Sheet) :

```bash
python3 scripts/pre_filter.py \
  --candidates /tmp/tt_candidates_raw.json \
  --sheet-data /tmp/tt_sheet_existing.json \
  > /tmp/tt_candidates_filtered.json
```

Lire le JSON résultant (`/tmp/tt_candidates_filtered.json`). Les candidats restants passent à l'étape 2 pour le filtrage LLM (éligibilité produit DNVB, prix sur landing, etc.). Les rejets sont loggués sur stderr.

## Étape 2 — Filtrage dur (règles utilisateur)

Pour chaque candidat du JSON filtré, évaluer avec jugement LLM :

**EXCLUSIONS CATÉGORIES** (rejet immédiat) :
- Compléments alimentaires (gummies, vitamines, ashwagandha, maca, berberine, etc.)
- Lampadaires (floor lamps)
- Tables (dining table, table lourde)
- Jouets bébés (baby toys)
- Garde-fous : domaines `*.myshopify.com`, ebooks/guides/gift cards, ésotérique (orgonite, chakra…), mobilier lourd (matelas, canapé)
- **Produit éligible uniquement** : landing PRODUIT d'un shop DNVB. EXCLURE méga-marques (shop > 1M visites/mois : ex. Volkswagen, Decathlon, LEGO, Zalando, REWE…), marketplaces, services (assurance, banque, RH/jobs), apps, médias, événements, listicles/blogs. Un même produit scalé par plusieurs pages (shop + pages « magazine »/« docteur ») = BON signal, le noter en Commentaire.
- Near-miss prix (29,90–29,99€) : EXCLU strictement, mais le mentionner dans le récap email.

**PAS DE PLAFOND DE NOMBRE** : tout candidat qui passe toutes les règles (exclusions ci-dessus + prix/coeff ci-dessous) est ajouté au Sheet. Ne JAMAIS tronquer la liste aux 3 ou 5 « meilleurs ».

**RÈGLES PRIX / COEFF** (X = Prix vente € / COGS €) :
- Prix < 30€ → EXCLU
- Prix 30–40€ → X ≥ 4 requis
- Prix > 40€ → X ≥ 3.5 requis
- Prix en devise non-€ : convertir avant comparaison.

## Étape 3 — COGS (image search d'abord, AliExpress en fallback)

COGS = prix produit **seul** (hors shipping, comme les lignes existantes du Sheet). Conversion ¥→€ ≈ 0,128 (¥30 ≈ 3,84€). Toujours noter la source dans le Commentaire (« COGS 1688 ¥XX » ou « COGS AliExpress XX€ »).

Pour chaque candidat retenu, utiliser l'image de l'ad creative (`image_url` du résultat `query_ads.py`) pour chercher le produit :

```bash
python3 scripts/cogs_image_search.py \
  --image-url "<image_url du candidat>" \
  --product-name "<nom EN du produit>"
```

Le script cherche sur 1688 via **Apify Image Search** (prioritaire), puis **AliExpress Image Search** via `aliexpress_image_search.py` en fallback.

Résultat JSON : `cogs_eur`, `product_url`, `method`, `confidence`.

- Si `method=apify_1688` : noter « COGS 1688 ¥XX (image search) » dans le Commentaire.
- Si `method=aliexpress_image` : noter « COGS AliExpress XX€ (image search) » dans le Commentaire.
- Si `method=not_found` → heuristique : COGS = Prix ÷ diviseur de niche (Cosmétique 6.5, Maison/Cuisine 5, Apparel mécanisme 6, Outdoor/Hobby/Sport 5, Santé soft 6.5, Puériculture 6, défaut 5.5), et écrire « COGS estimé (heuristique) » dans le Commentaire.

Recalculer X avec le COGS réel ; si X passe sous le seuil de l'étape 2 → retirer le produit.

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

## Étape 5 — Enrichissement par produit

- **Colonne « Ads » + blocs AD** : `search_ads(query="<domaine>", search_in="domain", status="active", limit=20, sort_by="reach", order="desc")` (30 crédits/produit) → « Ads » = nombre de résultats (plafonné : écrire « 20 »), top ads par reach → AD1–AD8. **URL ad = `trendtrack_url`** du résultat (lien direct vers la page TrendTrack de l'ad).
- **Concurrents** : `mcp__trendtrack__find_similar_shops(shop="<domaine>", limit=5)` (24 crédits) → les 2 premiers qui passent :
  - PAS marketplace (amazon, ebay, walmart, etsy, shein, temu, tiktokshop)
  - PAS méga-marque (traffic > 1M visites/mois ou FB likes > 1M ; ex. L'Oréal, Sephora, Douglas, Myprotein)
  - Format cellule : `nom (domaine) — Xk visites/mois, N ads`
  - Si les 5 sont tous des méga-marques : écrire « Aucun DNVB éligible — <noms> (méga-marques) ».

## Étape 6 — Écriture dans le Sheet (JAMAIS modifier les lignes existantes)

Ligne de 75 colonnes :
```
[FALSE, Nom, URL, Pays, Niche, Ads, Priorité, "", COGS, Prix, X,
 AD1_url, AD1_imp, AD1_date1, AD1_date2, … AD8 max (blocs de 4),
 …, Commentaire, Score, Conc1, Conc2]
```
- Colonnes 12–71 : blocs AD1..AD15 (url, impressions/spend, date 1, date 2) — remplir 8 ads max, le reste vide.
- Dates format `Mmm YY` (ex. `Sep 26`) **en texte forcé** : préfixer d'une apostrophe (`'Dec 25`), sinon Sheets convertit en vraie date avec l'année en cours (piège constaté). Date 1 = première vue, date 2 = dernière vue (si inconnue : mois en cours, l'ad est active).
- Impressions/spend format bucket : `10 M +`, `7 - 10 M`, `3 - 7 M`, `1 - 3 M`, `< 1 M`.
- URL ad : `trendtrack_url` du résultat `search_ads` (lien dashboard TrendTrack).
- Lignes classées par Priorité croissante puis par niche.
- **NE PAS utiliser `sheets append`** : la plage « utilisée » du Sheet s'étend à ~966 lignes (formatage historique), `values.append` écrirait en bas de la grille (ligne 967+). Trouver la dernière ligne L avec une valeur en colonne B (`sheets get <SHEET_ID> 'Products'!B1:B2000`), puis écrire à position exacte :
  ```bash
  $GAPI sheets update 1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM "Products!A<L+1>:BW<L+n>" --values '<JSON>'
  ```
  Relire ensuite B(L+1):B(L+n) pour vérifier.

## Étape 7 — Rapport par email

```bash
$GAPI gmail send --to autoecom77@gmail.com --subject "Recherche produit TrendTrack — <date>" --body "<résumé>"
```
Résumé : produits examinés / retenus / ajoutés, crédits consommés (check_credits avant + après), top 3 (nom, prix, X, score, priorité), lien : https://docs.google.com/spreadsheets/d/1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM/edit?gid=815136391

## Règles opérationnelles

- Crédits limités (10k/période, reset le 21, ~15 runs/mois) : moyenne cible ≤ 650 crédits/run, plafond dur 1 200. Coûts mesurés : `search_ads`=30, `find_similar_shops`=24, `check_credits`=0 → 90 + 54×N crédits pour N produits enrichis (5 ≈ 360, 10 ≈ 630). Enrichir par Score décroissant ; si le plafond est atteint avant la fin de la liste, arrêter proprement et noter les produits non traités dans l'email de récap. Mesurer avant/après et mettre la conso dans le récap ; < 500 restants → skip le run.
- Colonnes `Validation / Lancement` et `Kalodata` restent vides (remplissage manuel).
- Si le MCP trendtrack échoue (OAuth expiré) : email d'alerte court, AUCUN ajout au Sheet.
- Trendtrack en lecture uniquement (jamais brandtracker/favorites en écriture).
