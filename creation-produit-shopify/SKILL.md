---
name: creation-produit-shopify
description: "Use when creating Shopify draft products from Sheet rows."
version: 1.0.0
---

# Création Produit Shopify

Crée un produit en **brouillon (DRAFT)** sur le store Shopify à partir d'une ligne validée du Google Sheet « Tableau de Recherche Produit » : titre FR, variantes (couleurs/tailles/matières) issues du site source, prix (col J du Sheet), stock 500 par variante, images pertinentes récupérées par heuristique. Jamais de mise en ligne (status DRAFT garanti), jamais d'écriture dans le Sheet. Trigger : « créer le produit », « mets-le sur shopify », « creation produit », « draft shopify », « tester le produit ».

## Accès (déjà configurés sur cette machine)

- **Google Sheets via `gws`** (authentifié durablement) — wrapper :
  ```bash
  GAPI="$HOME/.hermes/venvs/gws/bin/python $HOME/.hermes/skills/productivity/google-workspace/scripts/google_api.py"
  ```
  (délègue automatiquement au CLI `gws`). Sheet « Tableau de Recherche Produit » : ID `1dpjf3fYBPG8eXgsbRnpi3QLdHdPwQB40W86Yz3pVPEM`, onglet `Products` (gid 815136391). Col A = `Validation / Lancement` (case à cocher ✔/TRUE), col B = Nom, col C = URL, col I = COGS, col J = Prix.
- **Shopify CLI 4.8.4** : store `aadunp-f2.myshopify.com` (« Alix Paris »), auth stockée par le CLI (`shopify store execute` la recharge automatiquement). Requiert `--allow-mutations` pour toute mutation.
- **Traduction texte dans images** : outil `image_generate` (provider `nous` = **Nous Portal**, `image_gen.provider: nous` dans config) — édition via `image_url` + prompt.
- **Fallback navigateur** : chrome-devtools MCP (CDP :9222, profil `~/.chrome-hermes`) si le site bloque curl.

## Balance scripté / interactif (validée avec l'utilisateur)

| Étape | Scripté | Interactif |
|---|---|---|
| Lecture Sheet → lignes cochées | `scripts/sheet_rows.py` | choix de la ligne + confirmation nom/URL |
| Langue du site (avant images) | heuristique dans `fetch_images.py` (lang, hreflang) | arbitrage si ambigu |
| Récup + filtre images | `scripts/fetch_images.py` | **revue obligatoire** : message avec TOUTES les images + liens réels, l'utilisateur valide/retire |
| Images avec texte (site non-FR) | `image_generate` (nous) traduit le texte en FR | l'image traduite est montrée dans la revue |
| Titre FR | protocole de traduction ci-dessous | — |
| Création draft + variantes + prix + stock | `scripts/create_draft.py` | **go final obligatoire** avant le write |

Arbitrage agent (jamais décidé seul) : fetch bloqué (429/WAF) → fallback navigateur ; manifeste suspect (< 3 images, dims bizarres) → rapporter et attendre.

## Étape 0 — Lire le Sheet (scripté)

```bash
python3 scripts/sheet_rows.py   # → JSON des lignes cochées (ligne, nom, URL, prix)
```

Si plusieurs lignes cochées : **demander laquelle** (liste + nom + URL). Si une seule : confirmer nom + URL avant de continuer. Refuser de partir sur une ligne non cochée.

## Étape 1 — Langue du site (AVANT tout téléchargement)

Sur la page produit : `<html lang=…>`, liens `hreflang` (surtout `hreflang="fr"`), sélecteur de langue, chemins `/fr/`.

- **Version FR existe** → utiliser l'URL FR comme source ; les images sont déjà FR, PAS de traduction d'images.
- **Site pas en français** → garder l'URL source, poser `translate_images=true` pour l'étape 3b.

## Étape 2 — Récupération des images (scripté, heuristique TESTÉE le 2026-10-06 sur luveon.com : 66 candidats → 18 gardés)

```bash
python3 scripts/fetch_images.py "<product_url>" --out manifest.json [--pools gallery,page] [--max 20]
```

**Sources** : `product.js`/`.json` (Shopify) images+media et og:image/JSON-LD → pool `gallery` ; balises `<img>` de la page → pool `page`. **Pools par défaut `gallery,page` mais PAS hardcodé** : selon la structure du site, l'agent ajuste (ex. drop `page` si la page est polluée par blog/widgets, ou un pool custom via `--include <regex>`).

**Filtres** (dans cet ordre) :
1. Format `.jpg/.jpeg/.png/.webp` vérifié par magic bytes ; svg/gif/vidéo rejetés
2. Denylist nom de fichier : logo, favicon, icon, sprite, payment, afterpay, klarna, paypal, stripe, badge, review, star, flag, avatar, placeholder, loading, header, footer, nav, cart, cookie, newsletter…
3. Hôtes first-party uniquement (`cdn.shopify.com`, domaine du shop) — tout tiers (ucarecdn, s3, widgets) rejeté
4. Géométrie : largeur ≥ 500px, plus petit côté ≥ 400px, ratio 0.5–2.5
5. Dédoublonnage : variantes de redimensionnement Shopify + basename canonique + hash du contenu
6. Plafond 20 images (ordre du manifeste = ordre d'upload, gallery d'abord)

Chaque rejet porte sa raison dans le manifeste. **Pitfall** : le CDN Shopify répond 429 à curl nu → headers navigateur complets obligatoires (déjà dans le script).

## Étape 3 — Revue utilisateur (interactif, BLOQUANT)

Message obligatoire contenant :
- **toutes les images gardées** avec leur **lien réel sur le site** (URL source), dimensions, pool d'origine
- résumé des rejets (raison + nombre)
- titre original → titre FR (étape 4), plan des variantes (noms/valeurs/nb) avec stock 500 et le **prix** qui sera appliqué (col J du Sheet)

L'utilisateur valide, retire des images ou modifie le titre. **Aucune création sans son go.**

## Étape 3b — Traduction des images à texte (uniquement si site non-FR)

Pour chaque image gardée : vérifier via vision si elle contient du texte (et sa langue). Si texte non-FR : éditer via `image_generate` (provider nous / Nous Portal) :

- `image_url` = copie locale de l'image, `prompt` = « Translate ALL visible text in this image to French. Keep the layout, composition, colors, style and every non-text element strictly identical. Only the text changes. »
- Enregistrer le résultat dans `assets/`, pointer l'entrée du manifeste vers le fichier local (`local_path`) — `create_draft.py` uploadera via staged upload.
- Si l'éditeur réécrit trop l'image (texte flou, éléments déformés), la signaler dans la revue au lieu de la refourguer silencieusement.

## Étape 4 — Titre FR (protocole de traduction exact — ne pas modifier)

Toujours passer le titre par ce protocole (PNG de contexte = image produit principale ou screenshot complet de la fiche ; ne sert qu'à la compréhension) :

```text
(Rôle : Tu es un traducteur professionnel et natif français, spécialisé dans les traductions e-commerce (fiches produits, sites, publicités), et expert en copywriting.
Mission: Ton objectif est de fournir une traduction fidèle, claire et naturelle sans aucune adaptation marketing ni ajout d'informations.
Contexte : Je vais te fournir deux éléments :
1. Un PNG de la fiche produit concurrente complète – uniquement pour que tu t'imprègnes du produit, de ses bénéfices et de son univers. Tu ne dois rien traduire de ce visuel. C'est seulement pour ta compréhension.
2. Le titre exact de la fiche produit concurrente – c'est ce titre que tu devras traduire.
Consignes de traduction :
 • La traduction doit être fidèle au sens exact.
 • Aucune adaptation marketing ni ajout d'informations.
 • Si une expression n'existe pas telle quelle en français, tu dois la traduire de la façon la plus proche et naturelle possible, tout en restant fidèle au sens.
 • Le rendu doit être clair, simple et fluide pour un natif français.
 • ⚠️ Si le titre contient un nom inventé ou un nom de produit de marque concurrente (ex : OrthoCare, MediShoes, etc.), tu dois proposer un nouveau nom original, différent, mais cohérent avec le produit. Ce nouveau nom doit : être court, mémorisable et crédible. Évoquer les bénéfices/valeurs du produit, en anglais (ou anglicisé) (car cela rend le nom plus stylé et adapté au e-commerce international), et rester dans le même esprit que l'original.
Format de réponse attendu : Titre original → Titre traduit)
```

Le titre FR obtenu est celui du produit Shopify. (Même pour un titre déjà FR : le protocole tourne quand même — il sert aussi à détecter les noms de marque concurrents à remplacer.)

## Étape 5 — Création du draft (scripté, derrière le go)

```bash
python3 scripts/create_draft.py \
  --manifest manifest.json \
  --title "<Titre FR>" \
  --options-from "<product_url>" \
  --price "<Prix col J>" \
  --stock 500 --store aadunp-f2.myshopify.com
```

Le **prix est obligatoire** : il vient de la colonne J (« Prix ») de la ligne Sheet sélectionnée (toujours disponible dans le JSON de `sheet_rows.py`). Refuser de lancer la création si le Sheet n'a pas de prix pour la ligne — le demander à l'utilisateur.

Ce que fait le script :
1. Idempotence : abort si un produit de même titre existe déjà
2. Lit les options source (`product.js` : ex. Taille XS–5XL, Couleur 9 valeurs) → `productOptions` = couleurs/tailles/matières
3. Une mutation `productSet(synchronous: true)` : `status: DRAFT`, `productOptions`, toutes les combinaisons en `variants` (chaque variante : `price` + `inventoryQuantities: [{locationId, quantity: 500}]`), `files` = les images du manifeste (`originalSource` = URL publique ; fichier local traduit → staged upload préalable)
4. Vérification par relecture : status DRAFT, nombre de variantes, **prix appliqué à chaque variante**, quantité 500 par variante, nombre de médias
5. Rend l'URL admin du produit

**Pitfalls API `productSet` (trouvés à l'usage, 2026-10-06)** :
- `OptionSetInput.values` = objets `[{name: v}]`, pas des strings (contrairement à `OptionCreateInput` de `productCreate`)
- `ProductVariantSetInput.optionValues` = `VariantOptionValueInput` : il faut **`optionName` + `name`** (« Option does not exist » sinon)
- `ProductSetInventoryInput` exige `name` (« available » ou « on_hand ») + `locationId` + `quantity`
- Pas de champ `count` sur la connexion `variants` dans le query de vérif ; compter les `nodes` (limite 250).
- **Contrat `price` (audit 2026-10-07)** : dans la version API par défaut du CLI (≥ 2025-10), `ProductVariant.price` est un **scalaire Money (string "39.90")**, pas un objet. Un sous-champ `price { amount currencyCode }` dans la sélection fait échouer **toute l'opération** (`Selections can't be made on scalars`) — même la mutation ne s'exécute pas. `create_draft.py` corrigé : sélection `price` scalaire + parsing tolérant (dict `amount` ou string) pour rester compatible ancienne version. C'est la dérive de ce contrat qui explique le produit sans prix du 2026-10-06 : toujours relire le prix de chaque variante après création.
- **Suppression impossible via l'API avec l'auth CLI actuelle** : `productDelete` exige la permission staff « delete products » (Access denied sinon). Pour nettoyer un produit de test : `productUpdate` avec `status: ARCHIVED` (vérifié OK), ou suppression manuelle dans l'admin.
- `ProductVariantSetInput.price` prend un décimal string ("39.90") ; le script normalise ("39,90" / "39.90€" acceptés) et refuse les valeurs ≤ 0.

**Prix** : obligatoire sur chaque variante, toujours issu de la colonne J (« Prix ») du Sheet — la vente sans prix défini est le bug à ne pas reproduire (produit créé le 2026-10-06 sans prix). Jamais de prix inventé : si la colonne est vide, demander à l'utilisateur.

## Règles opérationnelles

- `status: DRAFT` toujours — jamais publier automatiquement.
- Jamais d'écriture dans le Sheet (pas de write-back, validé).
- La création est un vrai write sur le store : go utilisateur obligatoire après la revue.
- Un produit par ligne cochée et validée ; réutiliser ce skill pour chaque ligne.
- Si `shopify store execute` perd l'auth ou manque des scopes (ex. `Access denied for locations field`, `read_inventory`) : relancer `shopify store auth -s aadunp-f2.myshopify.com --scopes write_products,write_themes,read_products,read_locations,read_inventory,write_inventory`. Pitfall headless : le CLI ouvre l'URL de consentement via `xdg-open` sans l'imprimer → shimmer `xdg-open` (script qui écrit "$@" dans un fichier) dans le PATH, lire l'URL capturée, l'ouvrir dans le navigateur persistant (session admin Shopify déjà loguée), cliquer « Update » sur la page « Update data access », puis vérifier avec une query `shop { name }` + `locations { nodes { id } }`.
- Seuil/filtre images ajustables par site (`--pools`, `--include`, `--max`) — documenter l'écart dans le rapport final.