# Police des sous-titres ASS

Les sous-titres SRT convertis en ASS utilisent **Trebuchet MS Bold** (`trebucbd.ttf`), jointe au MKV pour que tous les lecteurs l'affichent pareil.

Cette police appartient à Microsoft et n'est pas redistribuée ici. Copiez-la depuis un Windows (`C:\Windows\Fonts\trebucbd.ttf`) ou le paquet `ttf-mscorefonts-installer` :

```bash
cp /usr/share/fonts/truetype/msttcorefonts/trebucbd.ttf fonts/trebucbd_0.ttf
```

Sans ce fichier, MouFlencoder fonctionne quand même : les ASS sont créés sans police jointe et le lecteur prend une police de remplacement.
