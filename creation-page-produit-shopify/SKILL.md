---
name: creation-page-produit-shopify
description: "Use when replicating a competitor product page in a duplicated Shopify theme."
version: 1.2.0
---

# Création Page Produit Shopify (thème dupliqué)

Construit la **page storefront** d'un produit déjà créé (skill `creation-produit-shopify`) : duplique le thème principal, le renomme du nom du produit, et **copie-colle intégralement** la fiche concurrente dans la copie — header/footer du thème conservés, tout le reste = design exact de la fiche source.

**Doctrine « copier-coller intégral »** (vérifiée le 2026-10-08 sur « Legging 3D anti-cellulite ») : mêmes images aux mêmes positions — les médias du produit (déjà les fichiers d'origine, mappés par nom) rendus via `product.media`, les visuels absents (bannières promo, icônes paiement, logos presse, GIF, cartes-avis, photos avant/après) rapatriés du CDN source dans les assets du thème ; textes verbatim si la source est FR (seul le nom de marque concurrent est remplacé par le nom du produit), sinon traduits via le protocole exact (Étape 4) ; buy box branchée sur les **vraies** variantes ; galerie = **tous** les médias. Ne jamais improviser, réécrire ou « s'inspirer » : la page cible = la fiche source, à l'identique.

Le THÈME copie reste `UNPUBLISHED` (validation via `?preview_theme_id=` sur le domaine myshopify) ; le PRODUIT doit être publié sur le canal Online Store pour que sa vraie page soit vérifiable (cf. Étape 7 + Pitfalls). Dépendance : le produit Shopify doit exister (sinon lancer `creation-produit-shopify` d'abord).

## Accès (déjà configurés sur cette machine)

- **Google Sheets via `gws`** (authentifié durablement) — wrapper :
  ```bash
  GAPI="$HOME/.hermes/venvs/gws/bin/python $HOME/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
  ```
  Sheet « Tableau de Recherche Produit » : ID `1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM`, onglet `Products`. Col A = `Validation / Lancement` (case à cocher ✔/TRUE), col B = Nom, col C = URL source.
- **Shopify CLI 4.8.4** : store `aadunp-f2.myshopify.com` (« Alix Paris »), thème principal `shrine-theme-pro` (role `MAIN`). Scopes : `write_themes,read_themes,read_products,write_products`. Toute mutation via `shopify store execute` passe par `--allow-mutations`.
- **Traduction** : l'agent applique lui-même le protocole Étape 4 (images de contexte via `vision_analyze`). Texte dans les images → `image_generate` (provider `nous`).
- **Capture de page** : `browser_exec` (CDP :9222, profil `~/.chrome-hermes`) pour les screenshots pleine page ; fallback `web_extract` pour le texte brut.

## Balance scripté / interactif (même contrat que `creation-produit-shopify`)

| Étape | Scripté | Interactif |
|---|---|---|
| Lecture Sheet → lignes cochées | `sheet_rows.py` (skill creation) | choix de la ligne + confirmation nom/URL |
| Produit Shopify + langue source | queries / heuristique | arbitrage si ambigu |
| Capture fiche source + plan de page | `browser_exec` + `page_plan.json` | validation du plan (ordre des blocs) |
| Traduction FR des blocs | protocole Étape 4 | — |
| Duplication + renommage du thème | `scripts/theme_dup.py` | — |
| Construction + push des fichiers | `scripts/theme_push.py` | **revue obligatoire** sur le lien preview |
| Liaison produit ↔ template | `theme_push.py --bind-product` | go final obligatoire |
| Publication produit (canal Online Store) | query `status`/`publishedAt` | switch admin « Manage publishing » + Save |

Arbitrage agent (jamais décidé seul) : fetch bloqué (429/WAF) → fallback navigateur ; page source sans structure exploitable → rapporter et attendre ; thème déjà nommé du produit → réutiliser (idempotence), jamais dupliquer deux fois.

## Étape 0 — Lire le Sheet (scripté)

```bash
python3 ~/.hermes/skills/ecommerce/creation-produit-shopify/scripts/sheet_rows.py
```

Plusieurs lignes cochées → **demander laquelle** (liste + nom + URL). Une seule → confirmer nom + URL avant de continuer. Refuser de partir sur une ligne non cochée.

## Étape 1 — Vérifier que le produit Shopify existe (scripté)

Query `products(first: 5, query: "title:'<Nom FR>'")` → récupérer `id`, `handle`, `title`, médias. **Produit absent** → lancer `creation-produit-shopify` d'abord (ou demander) ; ne jamais construire une page pour un produit inexistant.

## Étape 2 — Langue du site source

Même heuristique que `creation-produit-shopify` : `<html lang=…>`, liens `hreflang` (surtout `hreflang="fr"`), chemins `/fr/`.

- **Version FR existe** → utiliser l'URL FR comme source : les textes sont déjà FR, pas de traduction.
- **Site pas en français** → garder l'URL source et traduire chaque bloc (Étape 4).

## Étape 3 — Capturer la fiche concurrente (design + textes)

1. **PNG pleine page** de la fiche source (`browser_exec`, screenshot full-page) → `assets/fiche-source.png`. Sert de contexte visuel pour la traduction ET de référence design.
2. Extraire le **plan de page** `page_plan.json` : chaque bloc visible, **dans l'ordre**, avec :
   - `type` : `hero` | `titre` | `paragraphe` | `liste` | `image` | `banniere` | `temoignage` | `spec` | `faq` | `garantie` | `cta`…
   - `text` (avec marqueurs **gras** conservés), `images` (URLs réelles du site), `style` (couleurs, polices, espacements, mise en page, ordre des colonnes)
   - le bloc `cta` = le **buy box** de la fiche source (sélecteur de variante + bouton d'achat).
3. Critère de fin : chaque bloc visible du PNG est présent dans le plan, dans le même ordre, sans trou.
4. **Cartographie des images** (copier-coller littéral) : dumper `[...document.images]` avec `x, y, w, h` triés → pour chaque image de la fiche, retrouver l'image correspondante dans les médias du produit **par nom de fichier** (l'étape creation conserve les noms d'origine : `21.png`, `1.1.jpg`, `gempages_…`). Les images absentes des médias (bannières promo, icônes de paiement, logos presse, GIF, cartes-avis) → **télécharger depuis le CDN source** (`curl` avec User-Agent navigateur) dans `build/assets/` et pousser dans les assets du thème (`asset_url`). Ne jamais improviser une image qui existe : la fiche = les mêmes visuels aux mêmes positions.

## Étape 4 — Traduction FR (protocole exact — ne pas modifier)

À appliquer **si la source n'est pas en français** (bloc par bloc). Deux éléments par bloc :
1. le PNG de la fiche produit concurrente complète — uniquement pour la compréhension, ne rien traduire dedans ;
2. la capture d'écran de la portion textuelle du bloc — c'est ce texte qu'on traduit.

```text
Contexte : Je vais te fournir deux éléments :
1. Un PNG de la fiche produit concurrente complète – uniquement pour que tu t’imprègnes du produit, de ses bénéfices et de son univers. Tu ne dois rien traduire de ce visuel. C’est seulement pour ta compréhension.
2. Une capture d’écran d’une partie textuelle spécifique de la fiche produit concurrente – c’est ce texte que tu devras traduire.
Consignes de traduction :
- La traduction doit être fidèle au sens exact.
- Aucune adaptation marketing ni ajout d’informations.
- Si une expression n’existe pas telle quelle en français, tu dois la traduire de la façon la plus proche et naturelle possible, tout en restant fidèle au sens. Ton objectif est de produire une traduction qui soit fidèle ET percutante (zéro mot-à-mot maladroit).
- Le rendu doit être clair, fluide et rédigé comme un texte pensé en français (et non comme une traduction).
- ⚠️ Si un mot ou un passage est en gras dans le texte original, tu dois aussi mettre en gras sa traduction.
Instructions :
1.Respect des blocs :
- Si le texte est structuré en blocs distincts (titre, paragraphe, liste à puces, sous-titres…), traduis bloc par bloc, sans couper davantage.
- Exemple :
  - Un titre complet → une traduction.
  - Un paragraphe entier → une traduction.
  - Une liste à puces → chaque puce séparée et traduite ligne par ligne
  - Une liste à puces → chaque puce séparée et traduite ligne par ligne
- Chaque bloc doit être affiché ainsi :
  
   Texte original (bloc)
  
   → Traduction en français (bloc)
  
  
2.Respect des blocs :
- Uniquement en français natif, clair et compréhensible.
- Ne jamais conserver d’anglais ou d’autre langue dans la traduction finale.
- Toujours privilégier la formulation française la plus idiomatique, plutôt qu’une traduction trop littérale.
- Reformuler si nécessaire pour correspondre à ce qu’un vrai copywriter français écrirait, sans jamais trahir le sens.
```

Règles de sortie : format `Texte original (bloc)` → `Traduction en français (bloc)` ; un bloc = un titre / un paragraphe / une puce ; le gras du source est repris en gras ; zéro langue étrangère dans la traduction finale. **Source déjà FR** : blocs repris tels quels, mais noms de marque concurrents remplacés par le nom du produit (même logique qu'au titre).

## Étape 5 — Dupliquer le thème (scripté)

```bash
python3 scripts/theme_dup.py --name "<Nom FR du produit>"
```

- Duplique le thème **MAIN** (`shrine-theme-pro`) et le nomme directement du produit (sinon la copie s'appelle « Copy of … »).
- Idempotent : un thème portant déjà exactement ce nom → réutilisé et signalé (`reused: true`).
- Attend `processing: false` avant de rendre la main.
- La copie reste `UNPUBLISHED` ; le thème MAIN n'est jamais touché.

## Étape 6 — Construire la page dans la copie (scripté, derrière le plan validé)

Règle d'or : **header et footer restent ceux du thème** — on ne touche ni `layout/`, ni `sections/header*`, ni `sections/footer*`, ni les `*-group.json`. Tout le corps de la page = design de la fiche source, bloc par bloc.

1. Arborescence `build/` :
   - `sections/pp-NN-<type>.liquid` — un fichier par bloc du plan, **numéroté dans l'ordre**. Chaque section : `{% schema %}` minimal (`{"name": "PP …"}`) + markup + styles scopés classes `pp-` (ne jamais écraser le CSS du thème).
   - `templates/product.<suffix>.json` — `{"sections": {"pp-01-hero": {"type": "pp-01-hero", "settings": {}}, …}, "order": ["pp-01-hero", …]}`.
   - `assets/` — images déco/icônes/bannières absentes de la galerie produit (les visuels produit se référencent en Liquid : `product.media`, `featured_image | image_url` ; les images traduites de l'étape 3b du skill creation sont uploadables telles quelles).
2. Le bloc `cta` contient un **vrai formulaire** `{% form 'product', product %}` (variante, quantité, add-to-cart) habillé comme la fiche source — jamais de bouton mort.
2b. **Buy box branchée sur le produit réel** (vérifié le 2026-10-07) :
   - Sélecteurs générés depuis `{% for option in product.options_with_values %}` — **jamais** d'options hardcodées (« Taille », « Couleur » : les vrais noms/valeurs viennent du produit, quel que soit leur nombre).
   - Les variantes sont embarquées en JSON serveur-rendered : `<script type="application/json" data-pp-variants>[{% for v in product.variants %}{"id": {{ v.id }}, "options": {{ v.options | json }}, "available": {{ v.available }}, "price": {{ v.price | money | json }}…}{% endfor %}]</script>` ; le JS trouve la variante correspondant à la sélection, cale `input[name=id]`, met à jour prix/barré, et passe le CTA en « Out of stock »/disabled si `available: false`. Pas de fetch `/products/<handle>.js` (casse sur les produits DRAFT).
   - Prix affichés via `variant.price | money` (jamais de prix hardcodé type « €39,90 »).
2c. **Galerie = TOUS les médias du produit** : `{% for media in product.media %}` sans `limit` (slides ET vignettes). Les médias sont déjà les fichiers de la fiche source (mappés par nom à l'étape 3) → la galerie de la page est automatiquement la galerie complète du produit.
3. Push + liaison produit ↔ template :

```bash
python3 scripts/theme_push.py --theme-id <gid theme> --dir build/ \
  --bind-product "<handle>" --suffix <suffix>
```

Le script pousse `sections/` **avant** `templates/` (ordre obligatoire, cf Pitfalls), relit chaque fichier poussé, puis cale `templateSuffix` sur le produit (`productUpdate`).

## Étape 7 — Revue + vérification (interactif, BLOQUANT)

- **La validation finale se fait TOUJOURS sur la vraie page du produit** : `https://aadunp-f2.myshopify.com/products/<handle>?preview_theme_id=<theme_id>` (le domaine `myshopify.com` préserve le param à la redirection ; le domaine primaire `alixparis.com` l'avale).
- **Prérequis** : produit `ACTIVE` **+ publié sur le canal Online Store** (un produit créé via API n'est sur aucun canal, `publishedAt: null` → sa page 404 ; procédure admin au Pitfall « 404 persistant »). Vérifier d'abord par query : `query { product(id:) { status publishedAt onlineStoreUrl } }`.
- **`?view=<suffixe>` sur un AUTRE produit = test de fumée uniquement** (sections présentes, 0 erreur Liquid) — **jamais** validation finale : la galerie et les photos d'avis rendent les médias de CET autre produit (compteurs trompeurs, avis sans photos → faux diagnostic de « mauvais produit » / galerie incomplète). L'aperçu de l'éditeur admin sans produit sélectionné substitue lui aussi un produit actif.
- **Checklist scriptée** sur la vraie page (`browser_exec` + `js`) :
  - galerie : `slideCount === thumbCount === product.media` total (aucun `limit`) ;
  - `brokenImgs === 0` (tous `document.images` chargées) et `liquidErrors === 0` ;
  - sélecteurs = **vraies options** du produit (ex. Taille XS→5XL × Couleur 9 valeurs) ; `input[name=id]` rempli d'un vrai id de variante ; prix = `variant.price | money` (ex. €39,90) ; CTA actif ;
  - toutes les sections du plan présentes **dans l'ordre** — vérifier par **classe** (`section[class^=pp-]`), pas par texte (espaces insécables → faux négatifs) ;
  - images hors galerie (avis, bannières, icônes, logos presse) : compteurs comparés au plan.
- Screenshot pleine page + comparaison **bloc par bloc** avec le PNG source : ordre, textes FR (gras conservé), **mêmes images aux mêmes positions**, couleurs/espacements, buy box fonctionnel.
- Header/footer = ceux du thème (jamais ceux de la fiche source).
- L'utilisateur valide ou demande des corrections → re-push des seuls fichiers concernés via `theme_push.py`.
- Rapport final : nom + id du thème (`UNPUBLISHED`), suffixe bindé, lien de la vraie page (preview_theme_id), lien admin du thème, écarts éventuels au plan documentés.

## Pitfalls (vérifiés le 2026-10-06 sur aadunp-f2)

- `themeDuplicate(id, name)` → le champ du payload est **`newTheme`** (pas `theme`) ; l'arg `name` nomme directement la copie. `themeUpdate(id, input: {name})` fonctionne aussi pour renommer.
- `themeFilesUpsert(themeId, files: [{filename, body: {type: TEXT|BASE64|URL, value}}])` : un JSON template dont un `type` de section n'existe pas encore est rejeté — « Section type 'X' does not refer to an existing section file » → **toujours pousser `sections/` avant `templates/`** (géré par `theme_push.py`).
- Échec **par fichier** : `upsertedThemeFiles` liste les succès, `userErrors` le reste — contrôler les deux, ne jamais se fier au seul exit code du CLI.
- Relecture : la connexion `theme.files` **sans** `filenames` est plafonnée/paginée et perd silencieusement les fichiers suivants (d'où un `readback_missing` faux) → toujours relire via `files(filenames: [...])` par lots (géré par `theme_push.py`).
- Après `themeDuplicate`, `processing: true` pendant quelques secondes ; `themeDelete` refuse tant que vrai (« You can't delete this theme until it has finished uploading. ») → poller `processing` avant toute suppression.
- `templateSuffix` du produit = le suffixe de `templates/product.<suffix>.json` (partie après `product.`, sans extension) ; écrit par `productUpdate(product: {id, templateSuffix})`, lisible dans `product { templateSuffix }`. Plafond : **25 caractères** max (sinon le nom est tronqué, ex. `bague-trefle-porte-bonheu`).
- `{% schema %}` : le champ `"name"` d'une section est aussi plafonné à **25 caractères** — « Invalid schema: name is too long (max 25 characters) » → le fichier est rejeté et l'ancien fichier homonyme reste en place (la relecture par `size` le détecte, `theme_push.py` compare maintenant les tailles).
- `themeFilesDelete(themeId, files: [String!]!)` : noms de fichiers **en strings simples** (pas des objets) ; payload `deletedThemeFiles { filename }` (champ à sous-sélection, sinon « Field must have selections »).
- Un produit en `DRAFT` renvoie **404 sur la boutique même avec `?preview_theme_id=`**, et le domaine primaire `alixparis.com` avale le param → **toujours prévisualiser via le domaine `myshopify.com`** (il préserve le param à la redirection 301). Preview partageable : produit `ACTIVE` ou `UNLISTED` **+ publié sur le canal Online Store** (cf. pitfall suivant) ; l'aperçu admin (Thèmes → Personnaliser) rend sans publication mais substitue un produit actif si le produit n'est pas visible.
- **404 persistant même `UNLISTED`/`ACTIVE`** : un produit créé via API n'est publié sur **aucun canal** (`publishedAt: null`) — le statut seul ne suffit pas. Vérifier `query { product(id:) { status publishedAt onlineStoreUrl } }` ; si `publishedAt: null`, publier via l'admin : fiche produit → panneau latéral → `s-internal-single-picker-field[label=Status]` → picker ACTIVE → bouton `s-internal-button[accessibilitylabel="Manage publishing"]` → switch `s-internal-switch` (canal « Online Store ») → Done → Save. `publishableResourcePublish` n'est pas accessible (scope `read/write_publications` refusé au CLI).
- Un template JSON = `{"sections": {…}, "order": […]}` ; toute section exige `{% schema %}` avec au moins `"name"`.
- Le rendu d'un template produit passe par `layout/theme.liquid` → header/footer automatiques ; inutile (et interdit) de les reconstruire dans les sections.
- Si `shopify store execute` perd l'auth ou les scopes : relancer `shopify store auth -s aadunp-f2.myshopify.com --scopes write_products,write_themes,read_products,read_themes,read_locations,read_inventory,write_inventory` — pitfall headless : shim `xdg-open` + navigateur persistant (voir skill `creation-produit-shopify`).
- CDN Shopify 429 sur curl nu → headers navigateur complets pour toute récupération d'image.

## Règles opérationnelles

- THÈME copie **jamais publié** (`UNPUBLISHED`) ; `themePublish` interdit sans demande explicite de l'utilisateur. Le PRODUIT, lui, doit être `ACTIVE` + publié sur le canal Online Store dès la vérification finale (sinon sa page 404 et la validation est impossible — cf Pitfall « 404 persistant »).
- Jamais d'écriture dans le Sheet (pas de write-back).
- Un produit = une copie de thème nommée du titre FR du produit ; les itérations suivantes se font dans cette même copie (idempotence).
- Go utilisateur obligatoire : (1) sur le plan de page + les traductions avant le premier write, (2) sur la preview avant le rapport final.
- Écritures uniquement sur la copie — le thème MAIN n'est jamais modifié.
- Chaque écart au plan source (bloc manquant, texte adapté, image différente) est documenté dans le rapport final, pas absorbé silencieusement.