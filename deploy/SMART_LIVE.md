# Pod Smart : mode ombre et passage en réel

Fiche de mise en route. Toutes les commandes se lancent sur le serveur d'Helsinki, par toi.
Aucune ne contient ni n'affiche l'adresse du wallet ou la clé.

## Ce qui est installé

| Service | Rôle | État après installation |
|---|---|---|
| `smart-mm-live` | papier (existant) : lit les fichiers de l'enregistreur | inchangé, continue de tourner |
| `smart-mm-shadow` | ombre : carnets en direct depuis Polymarket, même logique de cotation, vraie couche d'ordres, **rien n'est signé ni envoyé** | démarré |
| `smart-mm-real` | réel : mêmes ordres, signés avec la clé et envoyés | installé, **désactivé**, ne démarre pas sans armement |

Le mode ombre écrit chaque ordre qu'il aurait envoyé (pose, annulation, refus, exécution simulée)
dans `/var/lib/smart-shadow/journal/orders-AAAA-MM-JJ.jsonl`, et publie son instantané chiffré
sous `smart-dashboard/data-shadow.enc`. La page du tableau de bord l'affiche avec `?src=shadow` à la
fin de son adresse (lien « voir l'ombre » en haut de page), une fois la nouvelle page déployée.

## 1. Déployer (maintenant)

```
cd /opt/sig-predictions-cup
git fetch origin claude/project-thread-qlk8wu
git archive FETCH_HEAD smart/live deploy/smart-mm-shadow.service deploy/smart-mm-real.service deploy/install_smart_live.sh deploy/SMART_LIVE.md | sudo tar -x -C /opt/smart-mm
sudo bash /opt/smart-mm/deploy/install_smart_live.sh
```

Seuls les fichiers du mode ombre et du réel sont extraits : le code du papier (`smart/mm.py`,
le tableau de bord) reste celui déjà déployé. Le script attend 90 s, affiche l'état de l'ombre
et termine par la vérification en lecture seule (beaucoup de « NON » à ce stade, c'est normal).

## 2. Préparer le réel, sans clé (quand tu veux)

```
sudo nano /etc/smart-live/wallet.env
```

Deux lignes, sans le `#` :

```
POLYMARKET_FUNDER=0x...           l'adresse affichée sur ton profil Polymarket
POLYMARKET_SIGNATURE_TYPE=1       1 si tu te connectes par e-mail, 2 par un wallet de navigateur (MetaMask...), 0 wallet simple
```

Puis la vérification :

```
cd /opt/smart-mm && sudo /opt/sig-predictions-cup/.venv/bin/python -m smart.live check
```

Elle lit tes positions (sans rien afficher d'identifiant) et dit si le capital déjà engagé dans
le wallet dépasse le budget : dans ce cas le réel refusera de démarrer.

## 3. Le jour du passage en réel

Seulement après ton accord explicite, et une fois les protections installées (c'est le cas après l'étape 1).

**Poser la clé.** La clé privée s'exporte depuis les réglages de ton compte Polymarket (à confirmer
sur ton compte). La commande ci-dessous la lit sans l'afficher et ne la garde pas dans l'historique :

```
sudo bash -c 'umask 077; printf "Cle privee (rien ne s affiche) : "; IFS= read -rs K; printf "%s" "$K" > /etc/smart-live/key; unset K; echo'
```

Le fichier appartient à root, lisible par root seul. Le service réel la reçoit par systemd
(`LoadCredential`) sous un utilisateur jetable ; le mode ombre ne peut pas la lire.

**Vérifier** : `cd /opt/smart-mm && sudo /opt/sig-predictions-cup/.venv/bin/python -m smart.live check`
doit finir par `ready`. Elle dérive les identifiants API depuis la clé, lit le solde USDC, les
autorisations et les ordres ouverts. Elle n'envoie aucun ordre.

**Armer** (le budget est ici, pas dans le code) :

```
sudo bash -c 'printf "armed=yes\nbudget_usdc=100\nuntil=2026-12-31\n" > /etc/smart-live/ARMED; chmod 644 /etc/smart-live/ARMED'
```

**Démarrer et suivre** :

```
sudo systemctl start smart-mm-real
journalctl -u smart-mm-real -f
```

## Arrêter

```
cd /opt/smart-mm && sudo /opt/sig-predictions-cup/.venv/bin/python -m smart.live stop
sudo systemctl stop smart-mm-real
```

`stop` écrit `/etc/smart-live/STOP` et annule tous les ordres ouverts du wallet. Tant que ce
fichier existe, le réel ne redémarre pas. Pour reprendre : `sudo rm /etc/smart-live/STOP`.
Désarmer : `sudo rm /etc/smart-live/ARMED`. Retirer la clé : `sudo shred -u /etc/smart-live/key`.

## Ce que le réel vérifie tout seul

- Au démarrage : pas de STOP, armement valide (fichier à root, `armed=yes`, date non dépassée),
  clé présente, géoblocage « non bloqué », capital déjà engagé du wallet sous le budget, puis
  annulation de tout ordre resté ouvert.
- Avant chaque ordre : post-only (jamais preneur, sauf la sortie avant échéance en fill-and-kill),
  ordre au plus 10 $, capital engagé (positions + ordres ouverts) sous le budget.
- En continu : heartbeat toutes les 5 s (si le processus meurt, Polymarket annule ses ordres en
  10 s), carnets à jour (plus de 30 s sans mise à jour : tout est annulé), géoblocage toutes les
  15 min, positions du wallet comparées à celles du pod toutes les minutes.
- Coupe-circuits : -5 % du budget dans la journée, -15 % depuis le plus haut, valeur sous 60 $,
  géoblocage, positions incohérentes. Un coupe-circuit annule tout, garde les positions, et
  persiste après redémarrage. Toi seul le lèves :
  `cd /opt/smart-mm && sudo /opt/sig-predictions-cup/.venv/bin/python -m smart.live clear-halt`.
- Arrêt du service : annulation de tous les ordres.

## Limites connues

- Le chemin réel n'a jamais touché l'API de Polymarket : il est testé sur des réponses simulées
  et la signature d'ordre est vérifiée hors ligne avec py-clob-client 0.34.6. Premier passage
  conseillé : un seul marché, budget réduit, surveillé.
- Les exécutions réelles sont lues en interrogeant l'état de nos ordres chaque seconde (pas le
  canal websocket utilisateur) : jusqu'à 1 s de retard sur l'inventaire.
- Le format exact de la réponse du heartbeat n'est pas documenté dans le client : à confirmer au
  premier démarrage dans le journal.
