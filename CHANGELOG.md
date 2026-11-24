# Historique

## 1.0.0 — novembre 2026

Première version publique, après trois semaines en production sur une médiathèque de 51 To.

- **07/11** — Naissance : remplace Tdarr. Encodage `hevc_vaapi` 10 bits, file, registre, Corbeille, règles Tempo, garde thermique, interface web, première connexion depuis la page.
- **07/11** — Décodage GPU quand la source le permet : temps processeur divisé par 15, puce passée de ~90 à ~60 °C.
- **09/11** — Audit de 439 remplacements (0 problème) ; B-frames ; contrôle du nombre d'images ; alertes Telegram ; renommage Sonarr/Radarr.
- **10/11** — Abandon du qp fixe : **taille visée** par épisode (375 Mo / 22 min) + plafond de qualité `qp_max`.
- **13/11** — Repli `-reinit_filter 0` pour les sources dont la colorimétrie change en cours de flux.
- **14/11** — Saut automatique des sources sans gain possible (x265 léger, audio lourd) ; définition et état « traité » dans la Médiathèque.
- **16/11** — Application installable (PWA) ; tris multiples ; voie de décodage mixte GPU/processeur (un épisode : 20 min → 6 min).
- **17/11** — **Harmonisation des pistes sans réencodage** (HDR10 et Dolby Vision conservés) ; vraie pause qui gèle ffmpeg ; conversion M4V/MP4/TS/AVI en MKV avec contrôle strict des images.
- **18/11** — Médiathèque en cartes sur téléphone ; boutons « Ajouter » et « Harmoniser » par série et par saison.
- **19/11** — Calcul du qp avec l'audio propre à chaque fichier (erreur moyenne −3,25 → −0,78 qp) ; 2e essai automatique si la sortie est trop grosse.
- **20/11** — Envois groupés vers Sonarr et Emby pendant les gros lots.
