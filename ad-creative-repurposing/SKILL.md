---
name: ad-creative-repurposing
description: "Autonomous ad creative repurposing, localization, and decision-tree pipeline for competitor ads."
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [ecommerce, ads, creative, video, image, localization, vozo, nous, decision-tree]
    related_skills: [recherche-produit-trendtrack, creation-produit-shopify, vozo-translate-dub]
---

# Ad Creative Repurposing & Localization Pipeline

Pipeline autonome de transformation et localisation des créatives publicitaires concurrentes (Images et Vidéos) pour le marché français e-commerce.

Traite les produits du Google Sheet **« Tableau de Recherche Produit »** dont la case **Validation / Lancement** est cochée, extrait les $X$ premières publicités actives, et applique un arbre de décision déterministe avec synchronisation Google Drive.

---

## Arbre de Décision (Decision Tree Architecture)

```mermaid
flowchart TD
    Start["Étape 0: Lecture du Sheet (sheet_rows.py)"] --> CheckTicked{"Ligne cochée (TRUE)?"}
    CheckTicked -->|Non| Refuse["Refuser / Demander de cocher"]
    CheckTicked -->|Oui (Multiples)| AskUser["Demander à l'utilisateur quel produit traiter"]
    CheckTicked -->|Oui (Unique)| Confirm["Confirmer nom + URL produit"]
    
    AskUser --> ExtractAds["Extraire les 3 premières Ads (AD1, AD2, AD3)"]
    Confirm --> ExtractAds
    
    ExtractAds --> LoopAds["Pour chaque Ad (1 à 3)"]
    
    LoopAds --> FetchMedia["Résolution Média (0 crédit: medias.trendtrack.io / Chrome CDP :9222)"]
    FetchMedia --> TypeCheck{"Type de Média?"}
    
    %% Branche Image
    TypeCheck -->|Image| ImgText{"Texte détecté?"}
    ImgText -->|Aucun texte| ImgDirect["Téléchargement Direct (Raw)<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_raw.jpg"]
    ImgText -->|Texte présent| ImgLang{"Langue du texte = Français?<br/>(detector.py: Gemini 3.8 Flash)"}
    ImgLang -->|Oui (FR)| ImgDirect
    ImgLang -->|Non (Autre)| ORGen["OpenRouter Image Translation<br/>(seedream-5-0-flash / Prompt FR guidé)"]
    ORGen --> ImgSave["Sauvegarder Image Traduite<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_translated_fr.png"]
    
    %% Branche Vidéo (Vozo - 100% Intacte)
    TypeCheck -->|Vidéo| VidAudio["Analyse Audio Locale (faster-whisper 0 crédit)"]
    VidAudio --> VidLang{"Audio en Français?"}
    VidLang -->|Oui (FR)| VidDirect["Garder Raw Tel Quel (0 crédit)<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_raw.mp4"]
    VidLang -->|Non (Autre)| VozoJob["Pipeline Vozo Translate & Dub<br/>• Doublage FR clonage voix<br/>• Alignement vidéo-audio auto<br/>• Remplacement sous-titres FR"]
    VozoJob --> VozoPoll["Attente fin du job & Téléchargement MP4 + SRT"]
    VozoPoll --> VidSave["Sauvegarder Vidéo Localisée<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_dubbed_fr.mp4"]
    
    %% Clôture & Sync
    ImgDirect --> Manifest
    ImgSave --> Manifest
    VidDirect --> Manifest
    VidSave --> Manifest
    
    Manifest["Générer manifest.json"] --> SystemdSync["Sync One-Way rclone vers gdrive:AdCreatives (Service & Timer)"]
    SystemdSync --> Done["Rapport Utilisateur Récapitulatif"]
```

---

## Contrat Opérationnel (Scripté vs Interactif)

| Étape | Scripté | Interactif |
|---|---|---|
| **0. Lecture Sheet** | `sheet_rows.py` | Sélection de la ligne si plusieurs cochées + confirmation nom/URL |
| **1. Extraction Ads** | `ad_resolver.py` | Validation des ads trouvées (AD1..AD3) |
| **2. Détection Type & Langue** | `detector.py` (MIME + whisper + Gemini 3.8 Flash) | Arbitrage si ambigu |
| **3. Branche Image FR / Sans texte** | Téléchargement direct raw (0 crédit) | — |
| **4. Branche Image non-FR** | `openrouter_image_translator.py` via OpenRouter Image API | Revue de l'image traduite |
| **5. Branche Vidéo FR** | Téléchargement brut direct (raw, 0 crédit) | — |
| **6. Branche Vidéo non-FR** | `vozo_video_translator.py` via Vozo API | Confirmation avant lancement du job payant |
| **7. Sync Cloud** | `rclone-adcreatives-sync.service` | Notification avec chemins des fichiers générés |

---

## Procédure d'Exécution Standard

### Étape 0 — Lire le Sheet

```bash
python3 ~/.hermes/skills/ecommerce/ad-creative-repurposing/scripts/sheet_rows.py
```

- Si plusieurs lignes cochées : **demander immédiatement à l'utilisateur laquelle traiter** (liste avec `row`, `name`, `url`).
- Si une seule ligne cochée : confirmer le nom et l'URL avant de lancer.
- **Refuser formellement** de traiter une ligne non cochée (`Validation / Lancement != TRUE`).

### Étape 1 — Lancer le Pipeline d'Arbre de Décision

```bash
python3 ~/.hermes/skills/ecommerce/ad-creative-repurposing/scripts/repurpose_ads.py \
  --row <ROW_NUMBER> \
  --max-ads 3 \
  --output-dir /home/ubuntu/AdCreatives
```

*(En mode test/inspection sans consommer de crédits de traduction, ajouter `--dry-run`).*

---

## Gestion des Données & Cloud Sync

### Stockage Local
Tous les actifs sont stockés dans des dossiers dédiés par produit sur le serveur :
```text
/home/ubuntu/AdCreatives/<product_slug>/
├── manifest.json
├── ad_1_raw.mp4               # (si vidéo déjà en français)
├── ad_2_dubbed_fr.mp4         # (si vidéo traduite via Vozo)
├── ad_2_subtitles_fr.srt
├── ad_3_translated_fr.png     # (si image traduite via OpenRouter Image API)
└── ad_3_source.jpg            # (image originale source)
```

### Synchronisation Google Drive
Le dossier `/home/ubuntu/AdCreatives` est synchronisé en miroir unidirectionnel vers Google Drive (`gdrive:AdCreatives`).
- **Service systemd utilisateur** : `rclone-adcreatives-sync.service`
- **Timer quotidien** : `rclone-adcreatives-sync.timer` (tous les jours à 04:00)
- **Déclenchement immédiat** : Automatiquement exécuté à la fin de `repurpose_ads.py`.
