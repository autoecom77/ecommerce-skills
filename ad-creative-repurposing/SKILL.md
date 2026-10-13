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
    TypeCheck -->|Image| ImgScan["Analyse Vision (detector.py: Gemini 3.8 Flash)<br/>• Détection Texte & Langue<br/>• Détection Logo / Watermark"]
    ImgScan --> ImgLang{"Langue du texte = Français<br/>ou Aucun Texte?"}
    
    %% Sous-branche FR / Sans texte
    ImgLang -->|Oui (FR ou Sans Texte)| ImgLogoFR{"Logo détecté?"}
    ImgLogoFR -->|Non (0 logo)| ImgDirect["Téléchargement Direct (Raw - 0 crédit)<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_raw.jpg"]
    ImgLogoFR -->|Oui (Logo présent)| ImgDelogo["OpenRouter Inpainting Delogo<br/>(Prompt suppression logo seul)<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_delogoed.png"]
    
    %% Sous-branche Non-FR
    ImgLang -->|Non (Autre langue)| ImgLogoNonFR{"Logo détecté?"}
    ImgLogoNonFR -->|Non (0 logo)| ORGen["OpenRouter Image Translation<br/>(seedream-5-0-flash / Prompt FR guidé)"]
    ImgLogoNonFR -->|Oui (Logo présent)| ORGenDelogo["OpenRouter Translation + Delogo<br/>(Prompt FR + Suppression logo)"]
    ORGen --> ImgSave["Sauvegarder Image Traduite<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_translated_fr.png"]
    ORGenDelogo --> ImgSave
    
    %% Branche Vidéo
    TypeCheck -->|Vidéo| VidAudio["Analyse Audio Locale (faster-whisper 0 crédit)"]
    VidAudio --> VidLang{"Audio en Français?"}
    VidLang -->|Oui (FR)| VidDirect["Garder Raw (0 crédit)<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_raw.mp4"]
    VidLang -->|Non (Autre)| VozoJob["Pipeline Vozo Translate & Dub<br/>• Doublage FR clonage voix<br/>• Alignement vidéo-audio auto<br/>• Remplacement sous-titres FR"]
    VozoJob --> VozoPoll["Attente fin du job & Téléchargement MP4 + SRT"]
    VozoPoll --> VidSave["Sauvegarder Master Doublé<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_dubbed_fr.mp4"]
    
    VidDirect --> VidFilter["Dernière Étape Obligatoire FFmpeg (video_postprocessor.py)<br/>• Filtre de Nuit Anti-Bleu (colortemperature=5500:pl=1)<br/>• H.264 (yuv420p) + faststart"]
    VidSave --> VidFilter
    VidFilter --> VidRepurposed["Créative Finale Prête<br/>/home/ubuntu/AdCreatives/&lt;produit&gt;/ad_N_repurposed.mp4"]

    %% Clôture & Sync
    ImgDirect --> Manifest
    ImgDelogo --> Manifest
    ImgSave --> Manifest
    VidRepurposed --> Manifest
    
    Manifest["Mise à jour manifest.json (à chaque ad)"] --> SystemdSync["Sync One-Way rclone continue vers gdrive:AdCreatives (&lt; 1s par ad)"]
    SystemdSync --> Done["Rapport Utilisateur Récapitulatif"]
```

---

## Contrat Opérationnel (Scripté vs Interactif)

| Étape | Scripté | Interactif |
|---|---|---|
| **0. Lecture Sheet** | `sheet_rows.py` | Sélection de la ligne si plusieurs cochées + confirmation nom/URL |
| **1. Extraction Ads** | `ad_resolver.py` | Validation des ads trouvées (AD1..AD3) |
| **2. Détection Type & Langue** | `detector.py` (MIME + whisper + Gemini 3.8 Flash text & logo detection) | Arbitrage si ambigu |
| **3. Branche Image FR / Sans texte** | Téléchargement direct raw (0 crédit) si aucun logo ; OpenRouter Delogo (`ad_N_delogoed.png`) si logo présent | — |
| **4. Branche Image non-FR** | `openrouter_image_translator.py` via OpenRouter Image API (Traduction FR standard ou FR + suppression logo) | Revue de l'image traduite |
| **5. Branche Vidéo FR** | Téléchargement brut direct (raw, 0 crédit) | — |
| **6. Branche Vidéo non-FR** | `vozo_video_translator.py` via Vozo API | Confirmation avant lancement du job payant |
| **7. Sync Cloud Continue** | `sync_creatives.py` (One-way local vers Drive à chaque étape) | Notification avec chemins des fichiers générés |

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
├── ad_1_source.mp4            # (vidéo originale source téléchargée)
├── ad_1_raw.mp4               # (copie raw si vidéo déjà en français)
├── ad_1_repurposed.mp4        # (créative finale avec filtre de nuit 5500K)
├── ad_2_dubbed_fr.mp4         # (master doublé et synchronisé si vidéo traduite via Vozo)
├── ad_2_subtitles_fr.srt      # (sous-titres français exportés par Vozo)
├── ad_2_repurposed.mp4        # (créative finale avec filtre de nuit 5500K)
├── ad_3_translated_fr.png     # (si image traduite ± delogoée via OpenRouter Image API)
├── ad_3_delogoed.png          # (si image FR / sans texte avec logo concurrent supprimé)
├── ad_3_raw.jpg               # (si image FR / sans texte sans aucun logo)
└── ad_3_source.jpg            # (image originale source)
```

### Synchronisation Google Drive
Le dossier `/home/ubuntu/AdCreatives` est synchronisé en miroir unidirectionnel vers Google Drive (`gdrive:AdCreatives`).
- **Service systemd utilisateur** : `rclone-adcreatives-sync.service`
- **Timer quotidien** : `rclone-adcreatives-sync.timer` (tous les jours à 04:00)
- **Déclenchement immédiat** : Automatiquement exécuté à la fin de `repurpose_ads.py`.
