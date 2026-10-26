# V2FC — Guide utilisateur

V2FC migre des machines virtuelles **VMware vSphere** (vCenter ou ESXi) vers
**Huawei FusionCompute**, à chaud : la VM source reste allumée pendant la
copie, puis une bascule courte (« Migrer maintenant ») l'éteint, copie les
derniers changements et démarre la VM sur FusionCompute.

Ce guide suit le parcours dans l'ordre des onglets de l'interface.
L'installation est décrite dans `install/README.md`.

---

## 1. Principe

```
vCenter / ESXi ──(lecture VDDK, NBD chiffré)──▶ VM relais (dans FusionCompute)
                                                   │ écrit directement sur
                                                   ▼ les volumes attachés
                                         volumes FusionCompute ──▶ VM migrée
```

- La **VM relais** héberge V2FC. Elle tourne dans FusionCompute. Pendant la
  copie, les volumes de destination lui sont attachés, puis à la bascule ils
  sont rattachés à la nouvelle VM.
- Le **runner** est le service qui exécute les copies. Son état est affiché
  en haut à droite de chaque page (● vert : en ligne, ● rouge : hors ligne).
- Une migration passe par trois étapes :
  1. **copie initiale** (seed) : tous les blocs utilisés du disque ;
  2. **deltas** : seulement les blocs modifiés depuis la copie précédente, grâce
     au suivi des changements VMware (CBT) ;
  3. **bascule** : arrêt de la VM source, delta final, création de la VM sur
     FusionCompute.

## 2. Comptes et rôles

| Rôle | Peut |
|---|---|
| Lecture seule | consulter environnements, plans, migrations, logs et rapports |
| Opérateur | en plus : créer des environnements et des plans, lancer les migrations et les bascules |
| Administrateur | en plus : gérer les comptes (onglet **Utilisateurs**) |

- **Créer un compte** : onglet **Utilisateurs** → *Nouveau compte* (mot de
  passe initial de 10 caractères minimum, à transmettre à la personne).
- **Désactiver un compte** : bouton **Désactiver**. Les comptes ne sont pas
  supprimés, car les plans et les migrations gardent leur auteur.
- **Changer son mot de passe** : cliquer sur son adresse e-mail en haut à
  droite.
- Il reste toujours au moins un administrateur actif, et personne ne peut
  retirer ses propres droits d'administrateur.

## 3. Environnements

Onglet **Environnements** → *Ajouter un environnement*.

**Source — vCenter / ESXi** : hôte, port 443, un compte avec les droits de
lecture des VM, de création et suppression de snapshots et d'accès aux
disques (VDDK).

**Cible — FusionCompute (VRM)** : adresse du VRM, port **7443** (API REST),
et un compte dédié de type **« interconnexion d'interface »** avec le rôle
administrateur.

> **Important — compte FusionCompute.** Un compte d'interconnexion ne peut
> pas se connecter au portail, donc il ne peut pas changer son mot de passe.
> Si FusionCompute impose le changement du mot de passe initial, la
> connexion réussit mais tout appel est refusé (« le VRM exige un
> changement du mot de passe… », code 10000041). **Avant de créer le
> compte**, désactiver ce changement obligatoire dans la politique de mot de
> passe de FusionCompute (procédure dans `install/README.md`, section 5).

Le bouton **Tester** vérifie la connexion. La VM relais est détectée et
enregistrée automatiquement dès que l'environnement FusionCompute existe
(sous 10 minutes au plus).

## 4. Plan de migration

1. Onglet **Environnements** → **Voir les VM** sur le vCenter source.
   L'inventaire affiche pour chaque VM si elle est **migrable**. Les causes
   bloquantes sont expliquées : VM chiffrée, disques partagés ou RDM
   physiques, modèle, etc.
2. Cocher les VM, donner un **Nom du plan** → *Créer un plan de migration
   avec la sélection*.
3. Onglet **Plans de migration** → **Choisir une cible FusionCompute**, puis
   pour chaque VM :
   - le **datastore** de destination (l'espace libre est indiqué) ;
   - pour **chaque carte réseau** de la VM source, le réseau FusionCompute
     de destination, ou *ne pas recréer cette carte* ;
   - **conserver la MAC** : recrée la carte avec l'adresse MAC source
     (réservation DHCP, licence liée à la MAC…). Dans ce cas, ne jamais
     rallumer la VM source en même temps que la VM migrée sur le même réseau.
   - **Enregistrer**.

**Rafraîchir la pré-vérification** revérifie les VM du plan juste avant de
lancer, car une VM peut avoir changé depuis la création du plan.

## 5. Migration

Onglet **Migration**, VM par VM :

1. **Créer un job** : choisir la VM relais (proposée par défaut).
2. **Provisionner** : crée sur FusionCompute un volume par disque et
   l'attache à la VM relais. Le provisioning est refusé avec un message
   clair si le datastore n'a pas la place pour la taille provisionnée des
   disques, ou si la VM relais n'a plus d'emplacement de disque libre
   (25 par VM).
3. La **copie initiale démarre automatiquement** quelques secondes plus tard.
   La barre de progression indique le pourcentage, le volume copié, le débit
   et le temps restant. Seuls les blocs réellement utilisés sont copiés.
4. Une fois la copie terminée, la VM est **synchronisée**. Les deltas se
   lancent :
   - à la main : **Lancer un delta maintenant** ;
   - ou chaque jour à heure fixe : **Delta chaque jour à** *[heure]* →
     **Planifier** (heure locale, changements d'heure compris ; **Annuler**
     pour arrêter).

   Plus le dernier delta est récent, plus la bascule est courte.
5. Sous chaque VM : la **dernière synchronisation** (fin, durée, volume
   écrit, résultat) et l'**historique** de toutes les copies. Le détail
   technique est dans l'onglet **Logs**.

Pendant toute cette phase, la VM source reste en service.

## 6. Bascule

**Migrer maintenant** (cocher *redémarrer automatiquement la VM cible* si
souhaité) :

1. arrêt propre de la VM source (via VMware Tools) ;
2. delta final, automatique ;
3. création de la VM sur FusionCompute avec le même nombre de vCPU, la même
   mémoire et le même firmware (BIOS/UEFI) que la source, le type d'OS
   correspondant et les cartes réseau du plan, puis rattachement des
   volumes.

Si la finalisation est interrompue (erreur FusionCompute), corriger la cause
indiquée dans les Logs puis **Relancer la finalisation** : les étapes déjà
faites sont sautées.

**Annuler / rollback** (pendant ou après la bascule) : rallume la VM source. La VM
FusionCompute éventuellement créée n'est jamais supprimée automatiquement.

### Après la bascule — à faire dans la VM migrée

- Les disques sont attachés en **IDE**, qui fonctionne sans pilote. Installer
  les **outils FusionCompute** (pilotes virtio) dans la VM, puis passer les
  disques en VIRTIO dans FusionCompute pour les performances. Au-delà de
  3 disques, les suivants sont attachés en VIRTIO et ne sont visibles
  qu'une fois les outils installés.
- Les **cartes réseau** sont de type virtio : sous Windows, pas de réseau
  avant l'installation des outils.
- Une **adresse IP fixe** est liée à l'ancienne carte réseau : la reconfigurer
  sur la nouvelle carte.
- Désinstaller les VMware Tools.

## 7. Rapport de migration

Onglet **Plans de migration** → **Rapport**. Pour chaque VM du plan :
datastore et réseaux choisis, disques, chaque synchronisation (début, fin,
durée, volume écrit, résultat), VM créée sur FusionCompute et événements
clés. Le bouton **Imprimer / enregistrer en PDF** produit un document à
remettre.

## 8. Supprimer un plan

**Supprimer** sur le plan. La confirmation indique ce qui sera supprimé :

- les migrations du plan et leur historique ;
- pour les migrations **non terminées**, les volumes déjà copiés sur
  FusionCompute (détachés de la VM relais puis supprimés) ;
- les VM **déjà migrées** ne sont jamais touchées.

La suppression est refusée pendant une copie ou une bascule : attendre la
fin, ou faire un rollback.

Pensez à générer le rapport **avant** de supprimer le plan.

## 9. Dépannage

| Symptôme | Cause et solution |
|---|---|
| ● *runner hors ligne* | Le service d'exécution est arrêté : aucune copie ne démarre. Sur le serveur : `docker compose up -d runner`, puis `docker compose logs runner`. |
| « le VRM exige un changement du mot de passe… » (10000041) | Voir §3 : politique de mot de passe FusionCompute, puis réinitialiser le mot de passe du compte. |
| « nom d'utilisateur ou mot de passe incorrect » (10100103) | Identifiants FusionCompute erronés (attention au verrouillage après plusieurs échecs). |
| « ce compte ne peut pas utiliser l'API sur ce port » (10100380) | Le compte n'est pas de type interconnexion d'interface. |
| « espace insuffisant sur le datastore… » | Choisir un datastore plus grand dans la cible du plan. |
| « le relais porte déjà N disques… » | Terminer ou supprimer d'autres migrations en cours. |
| Copie en échec | Elle est relancée automatiquement après 5 minutes. Le message d'erreur est dans l'onglet Logs. |
| « CBT invalidé » | VMware a réinitialisé le suivi des changements (restauration de snapshot, par exemple) : une nouvelle copie complète est faite automatiquement. |

## 10. Limites connues

- Débit d'une copie : de l'ordre de 70 Mio/s par VM (NBD chiffré). Plusieurs
  VM peuvent être copiées en parallèle (4 par défaut).
- Disques de destination en IDE à la bascule (voir §6).
- Une seule VM relais par environnement FusionCompute : celle qui héberge
  V2FC.
