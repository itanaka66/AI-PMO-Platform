# Premiers pas avec AI-PMO

> Source : les versions japonaise et anglaise font foi. Les autres langues en
> sont des traductions.

---

## De quoi s'agit-il ?

Un outil pour confier à une IA le travail de gestion de projet (PMO).

Il peut par exemple :

- transformer l'enregistrement d'une réunion Teams en **compte rendu, automatiquement**
- en extraire **qui fait quoi et pour quand**, et l'enregistrer sous forme de tâches
- **relancer automatiquement** les tâches dont l'échéance est dépassée

Il suffit de choisir un « modèle », c'est-à-dire le plan du travail.
Aucune connaissance en programmation n'est nécessaire.

---

## À qui cela s'adresse-t-il ?

- **Étudiants** — apprendre la forme de la gestion de projet en s'en servant
- **PME** — disposer des méthodes sans PMO dédié
- **Grandes organisations** — harmoniser par des modèles ce que chaque service fait à sa façon

C'est gratuit. L'utilisation n'est pas facturée.

---

## Ce qu'il faut

| | Nécessaire | Coût |
|---|---|---|
| **Configuration simple** | Un ordinateur et une clé d'API d'un service d'IA | Usage de l'IA (à la consommation, faible) |
| **Configuration interne** | Docker, 16 Go de RAM ou plus, si possible un GPU | Gratuit (l'électricité seulement) |

> **Laquelle choisir ?**
> Pour essayer, la **configuration simple**.
> Si le contenu des réunions ne doit pas sortir de l'organisation, la
> **configuration interne**.

---

## Démarrer en trois étapes

### 1. Installer

Suivez [INSTALL.md](../../INSTALL.md).

- **Windows** — double-cliquez sur `AI-PMO-Setup.exe`
- **Mac / Linux** — lancez `./scripts/install.sh` dans un terminal
- **Docker** — lancez `./scripts/install-docker.sh`

### 2. Configurer

L'écran de configuration s'ouvre de lui-même après l'installation.
Répondez aux questions ; en cas de doute, Entrée reprend la valeur par défaut.

```
1) Où l'IA doit-elle s'exécuter ?   → 1 (cloud)
2) Choisissez un fournisseur d'IA   → 1 (OpenAI)
3) Saisissez votre clé d'API        → collez-la
4) Nom identifiant votre organisation → votre société, en minuscules
5) Activer la couche base de données ? → N
```

**Quatre fournisseurs sont proposés.** En cas d'hésitation, prenez OpenAI :
il gère aussi les embeddings, un seul réglage suffit donc.

| Fournisseur | Caractère |
|---|---|
| OpenAI | Le choix par défaut |
| Gemini | Traite les longues transcriptions à bas coût |
| Groq | Rapide, mais demande deux clés |
| OpenRouter | Une clé pour comparer de nombreux modèles |

**Obtenir une clé d'API**
Créez un compte chez le fournisseur choisi et émettez une clé.
C'est une longue chaîne. Ne la montrez à personne.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

Voir [PROVIDERS.md](../PROVIDERS.md) pour le détail.

### 3. Essayer

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

Si ceci s'affiche, c'est réussi :

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

---

## Ce qu'est un modèle

Un plan décrivant ce qui est fait, et dans quel ordre.
Un modèle correspond à une tâche de PMO.

```yaml
name: meeting_minutes          # son nom
trigger: "event:teams:meeting_ended"   # quand il s'exécute (fin de réunion)

steps:                         # ce qu'il fait
  - id: fetch_transcript       # 1. récupérer l'enregistrement
    adapter: teams

  - id: minutes                # 2. faire rédiger le compte rendu par l'IA
    llm: { profile: default }

  - id: register_jira          # 3. enregistrer les tâches
    adapter: jira
```

Si le travail change, on change de modèle.
**La manière même dont l'IA est utilisée change avec le modèle.**

---

## Commandes courantes

```bash
aipmo setup       # refaire la configuration
aipmo validate <fichier>   # vérifier qu'un modèle est correct
aipmo run <fichier>        # l'exécuter
aipmo adapters    # lister les outils connectés
aipmo doctor      # vérifier que les connexions fonctionnent
```

---

## Ce qu'il faut savoir côté sécurité

**Votre clé d'API est enregistrée dans `.env`,** pas dans `config.yaml`.
Les fichiers de configuration se partagent entre collègues et finissent dans
Git ; la clé est donc tenue à l'écart.

**Les données internes ne sortent pas.** Les données de chaque organisation
sont stockées séparément, et atteindre celles d'une autre n'est techniquement
pas possible.

**Rien n'est publié automatiquement.** Un mécanisme permet de partager
publiquement le savoir-faire, mais il exige toujours l'accord d'une personne.
Aucun programme ne peut publier de lui-même.

---

## En cas de problème

**Je tape `aipmo` et j'obtiens « commande introuvable »**
Sur Mac ou Linux, lancez ceci puis rouvrez le terminal :
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**Sous Windows, double-cliquer sur le `.ps1` ne fait rien**
Double-cliquez plutôt sur `install.bat`.

**J'ai oublié de saisir ma clé d'API**
Relancez `aipmo setup`.

**L'antivirus bloque l'installateur**
Les fichiers non signés peuvent déclencher un avertissement. Si cela vous gêne,
utilisez la version Mac / Linux ou celle en Docker.

Plus de détails dans [INSTALL.md](../../INSTALL.md).

---

## À lire ensuite

- [INSTALL.md](../../INSTALL.md) — l'installation en détail
- [MOBILE.md](../MOBILE.md) — l'utiliser depuis un téléphone
- [PROVIDERS.md](../PROVIDERS.md) — choisir un fournisseur d'IA
- [AGENTS.md](../AGENTS.md) — laisser l'IA décider elle-même
- [TEAMS.md](../TEAMS.md) — relier les enregistrements Teams
- [JIRA-SLACK.md](../JIRA-SLACK.md) — créer des tickets Jira et notifier sur Slack
- [SCHEDULER.md](../SCHEDULER.md) — exécuter automatiquement à heure fixe
- [AGILE.md](../AGILE.md) — rendre compte des sprints
- [README.md](../../README.md) — le fonctionnement, pour les développeurs
- `templates/examples/` — modèles d'exemple
