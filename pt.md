# Primeiros passos com o AI-PMO

> Origem: as versões em japonês e em inglês são as originais. Os demais idiomas
> são traduções.

---

## O que é isto?

Uma ferramenta para entregar a uma IA o trabalho de gestão de projetos (PMO).

Por exemplo, ela consegue:

- transformar a gravação de uma reunião do Teams em **ata, automaticamente**
- extrair dessa ata **quem faz o quê e até quando**, registrando como tarefas
- **cobrar automaticamente** as tarefas que passaram do prazo

Basta escolher um "modelo", que é a planta do trabalho.
Não é preciso saber programar.

---

## Para quem serve?

- **Estudantes** — aprender a forma da gestão de projetos usando na prática
- **Pequenas empresas** — ter os métodos sem um PMO dedicado
- **Grandes organizações** — alinhar por modelos o que cada área faz do seu jeito

É gratuito. Não há cobrança pelo uso.

---

## O que é necessário

| | Requisitos | Custo |
|---|---|---|
| **Configuração simples** | Um computador e uma chave de API de um serviço de IA | Uso da IA (por consumo, baixo) |
| **Configuração interna** | Docker, 16GB de RAM ou mais, de preferência uma GPU | Gratuito (só a energia) |

> **Qual escolher?**
> Para experimentar, a **configuração simples**.
> Se o conteúdo das reuniões não pode sair da organização, a **configuração
> interna**.

---

## Começar em três passos

### 1. Instalar

Siga o [INSTALL.md](../../INSTALL.md).

- **Windows** — dê dois cliques em `AI-PMO-Setup.exe`
- **Mac / Linux** — execute `./scripts/install.sh` no terminal
- **Docker** — execute `./scripts/install-docker.sh`

### 2. Configurar

A tela de configuração abre sozinha ao terminar a instalação.
Responda às perguntas; na dúvida, Enter aceita o valor padrão.

```
1) Onde a IA deve rodar?          → 1 (nuvem)
2) Escolha um provedor de IA      → 1 (OpenAI)
3) Informe sua chave de API       → cole
4) Nome que identifica sua organização → sua empresa, em minúsculas
5) Ativar a camada de banco de dados?  → N
```

**São quatro provedores para escolher.** Na dúvida, escolha OpenAI: ele também
tem embeddings, então uma única configuração resolve.

| Provedor | Característica |
|---|---|
| OpenAI | A escolha padrão |
| Gemini | Processa transcrições longas de forma barata |
| Groq | Rápido, mas exige duas chaves |
| OpenRouter | Uma chave para comparar muitos modelos |

**Como obter uma chave de API**
Crie uma conta no provedor escolhido e emita uma chave.
É uma sequência longa. Não mostre a ninguém.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

Mais detalhes em [PROVIDERS.md](../PROVIDERS.md).

### 3. Testar

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

Se aparecer isto, deu certo:

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

---

## O que é um modelo

Uma planta que descreve o que é feito e em que ordem.
Cada modelo corresponde a uma tarefa de PMO.

```yaml
name: meeting_minutes          # nome
trigger: "event:teams:meeting_ended"   # quando roda (ao fim de uma reunião)

steps:                         # o que faz
  - id: fetch_transcript       # 1. buscar a gravação
    adapter: teams

  - id: minutes                # 2. a IA redige a ata
    llm: { profile: default }

  - id: register_jira          # 3. registrar as tarefas
    adapter: jira
```

Se o trabalho muda, troca-se o modelo.
**O próprio modo de usar a IA muda junto com o modelo.**

---

## Comandos frequentes

```bash
aipmo setup       # refazer a configuração
aipmo validate <arquivo>   # verificar se o modelo tem erros
aipmo run <arquivo>        # executar
aipmo adapters    # listar as ferramentas conectadas
aipmo doctor      # conferir se as conexões funcionam
```

---

## O que saber sobre segurança

**Sua chave de API fica em `.env`,** não em `config.yaml`. Arquivos de
configuração são compartilhados com colegas e vão para o Git, então a chave
fica separada.

**Os dados internos não saem.** Os dados de cada organização ficam em locais
separados, e alcançar os de outra não é tecnicamente possível.

**Nada é publicado automaticamente.** Existe um mecanismo para compartilhar
conhecimento publicamente, mas ele sempre exige a aprovação de uma pessoa.
Nenhum programa publica por conta própria.

---

## Quando algo não funciona

**Digito `aipmo` e aparece "comando não encontrado"**
No Mac ou Linux, execute isto e reabra o terminal:
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**No Windows, dar dois cliques no `.ps1` não faz nada**
Dê dois cliques em `install.bat`.

**Esqueci de informar a chave de API**
Execute `aipmo setup` novamente.

**O antivírus bloqueia o instalador**
Arquivos sem assinatura podem gerar um aviso. Se isso preocupa, use a versão
para Mac / Linux ou a de Docker.

Mais detalhes em [INSTALL.md](../../INSTALL.md).

---

## O que ler em seguida

- [INSTALL.md](../../INSTALL.md) — a instalação em detalhe
- [MOBILE.md](../MOBILE.md) — usar pelo celular
- [PROVIDERS.md](../PROVIDERS.md) — como escolher o provedor de IA
- [AGENTS.md](../AGENTS.md) — deixar a IA decidir sozinha
- [TEAMS.md](../TEAMS.md) — conectar as gravações do Teams
- [JIRA-SLACK.md](../JIRA-SLACK.md) — registrar tarefas no Jira e avisar no Slack
- [SCHEDULER.md](../SCHEDULER.md) — executar automaticamente em horários definidos
- [README.md](../../README.md) — como funciona, para desenvolvedores
- `templates/examples/` — modelos de exemplo
