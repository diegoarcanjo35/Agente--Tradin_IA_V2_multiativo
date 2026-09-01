# Avisos de Terceiros (Third-Party Notices)

Este projeto vendoriza localmente uma única dependência de frontend. Nenhum
código remoto é carregado em tempo de execução do navegador — o arquivo
abaixo é servido exclusivamente pelo próprio backend FastAPI, a partir de
`frontend/vendor/`, através do mount `/static` já existente
(`app/api/main.py::create_app`).

## TradingView Lightweight Charts™

- **Nome da biblioteca**: `lightweight-charts`
- **Versão exata**: `4.1.4` (última versão estável da série 4.x — a série
  5.x removeu o build standalone/UMD para navegador, incompatível com a
  arquitetura sem bundler deste projeto; ver nota abaixo)
- **URL/origem oficial**: pacote npm oficial
  `https://registry.npmjs.org/lightweight-charts/-/lightweight-charts-4.1.4.tgz`
  (mantenedor: TradingView, Inc. — repositório oficial
  `https://github.com/tradingview/lightweight-charts`)
- **Arquivo obtido**: `dist/lightweight-charts.standalone.production.js`
  (build de produção standalone/UMD, minificado, expõe `window.LightweightCharts`
  globalmente — extraído diretamente do tarball oficial do npm, nunca colado
  ou editado à mão)
- **Nome do arquivo vendorizado**: `frontend/vendor/lightweight-charts.standalone.production.js`
- **SHA-256 do arquivo vendorizado** (calculado localmente após extração):
  `ec4bdfaafb53273e176520caac61ef0f6b69a40b395df7be2445aac33713625d`
- **Integridade do tarball de origem verificada**: o tarball baixado
  (`lightweight-charts-4.1.4.tgz`) teve seu SHA-512 conferido byte a byte
  contra o valor `integrity` publicado pelo registro oficial do npm
  (`sha512-jsQOK27a3wiw/Db3Eoo3VX93LGovXA/sOWHVEiEosGOOGtxSSuIWTYVebjRKGK0SWkkUwI8AHQ4j7HZSKm7fxA==`)
  antes da extração — coincidência exata confirmada.
- **Data da obtenção**: 2026-09-01
- **Licença aplicável**: Apache License 2.0. Texto completo incluído em
  `frontend/vendor/LICENSE.lightweight-charts.txt` (extraído do mesmo
  tarball oficial, arquivo `LICENSE` do pacote).
- **Atribuição**: exigida — ver seção "Obrigação de atribuição visível"
  abaixo. Implementada de forma discreta em `frontend/index.html`, logo
  abaixo do card do gráfico (`<p class="chart-attribution">`), sem alterar
  o layout geral do painel.

### Obrigação de atribuição visível (auditoria da Fase 3.1, gate 1)

Inspeção completa do tarball oficial `lightweight-charts-4.1.4.tgz`
confirma:

- **Arquivos presentes no pacote**: `dist/*`, `index.cjs`, `LICENSE`,
  `package.json`, `README.md`. **Não há arquivo `NOTICE`** neste pacote
  npm, apesar de o próprio `README.md` do pacote referenciá-lo (ver
  citação abaixo) — uma inconsistência do pacote tal como publicado pelo
  fornecedor, não algo inventado ou omitido por este projeto.
- **`LICENSE`**: texto integral e não modificado da Apache License 2.0
  (boilerplate padrão, "Copyright 2023 TradingView, Inc."). Por si só,
  a Apache 2.0 pura **não** exige atribuição visível na interface de um
  aplicativo que apenas *usa* a biblioteca (não há cláusula de
  "advertising"/atribuição em tela como em licenças BSD-4-cláusulas).
- **`README.md` do pacote, seção "License"** (citação literal, nunca
  parafraseada): *"This license requires specifying TradingView as the
  product creator. You shall add the "attribution notice" from the NOTICE
  file and a link to <https://www.tradingview.com/> to the page of your
  website or mobile application that is available to your users. As
  thanks for creating this product, we'd be grateful if you add it in a
  prominent place."* — esta é uma condição **adicional publicada pelo
  próprio fornecedor**, além do texto puro da licença, e vai além do que a
  Apache 2.0 exigiria sozinha.
- **Cabeçalho do build vendorizado**: `/*! @license TradingView Lightweight
  Charts™ v4.1.4 Copyright (c) 2024 TradingView, Inc. Licensed under
  Apache License 2.0 https://www.apache.org/licenses/LICENSE-2.0 */` —
  preservado integralmente, nunca editado.
- **`package.json`**: `"license": "Apache-2.0"`, `"author": "TradingView,
  Inc."` — nenhuma instrução de atribuição adicional além da já citada no
  README.

**Respostas objetivas**:

1. *Existe obrigação de exibir atribuição visível?* **Sim** — declarada
   pelo próprio `README.md` do pacote oficial (citação acima), não pela
   Apache 2.0 isoladamente.
2. *Existe texto ou link obrigatório?* O **link é literal e explícito**:
   `https://www.tradingview.com/`. O texto exato da "notice" **não está
   disponível** — o `NOTICE` file que o README referencia não existe
   neste pacote. Nunca foi inventada uma redação jurídica para substituí-
   lo; a atribuição implementada usa apenas o nome do produto e o link
   literal fornecidos pelo próprio texto oficial.
3. *`THIRD_PARTY_NOTICES.md` sozinho satisfaz a licença?* **Não
   totalmente.** Ele satisfaz a obrigação de "fornecer cópia da licença e
   preservar avisos" (Apache 2.0, seção 4), mas o README oficial pede
   algo especificamente **"to the page of your website... available to
   your users"** — visibilidade na própria interface, não apenas num
   arquivo de documentação do repositório. Por isso a atribuição também
   foi adicionada em `frontend/index.html`.
4. *Todos os avisos originais do arquivo foram preservados?* **Sim** — o
   arquivo vendorizado nunca foi editado (extraído byte a byte do tarball
   verificado); `LICENSE.lightweight-charts.txt` é cópia exata e completa
   das 201 linhas do arquivo original.

### Nota sobre a escolha da versão (4.1.4, não a mais recente)

A versão mais recente publicada no npm no momento da obtenção era `5.2.1`,
mas a série 5.x do pacote **não publica mais um build standalone/UMD para
navegador** (apenas ESM `.mjs`), o que exigiria `<script type="module">` e
`import` — incompatível com a arquitetura deste projeto (frontend sem
bundler, um único `<script src="...">` clássico carregando `app.js`). A
série `4.x` (última: `4.1.4`) é a versão mais recente que ainda publica
`dist/lightweight-charts.standalone.production.js`, exatamente o formato
necessário. `4.1.4` foi fixada explicitamente (nunca `latest`, nunca uma
URL/tag mutável).

### Como foi obtido (rastreabilidade do processo)

1. Consulta ao registro oficial do npm (`registry.npmjs.org`) para
   confirmar a versão exata e o hash de integridade publicado.
2. Download do tarball oficial (`.tgz`) diretamente do npm — nunca de um
   mirror/CDN não-oficial.
3. Verificação do SHA-512 do tarball contra o valor publicado pelo npm
   (`integrity`) — falha nessa verificação teria interrompido o processo.
4. Extração, do próprio tarball, apenas dos três arquivos necessários:
   `dist/lightweight-charts.standalone.production.js`, `LICENSE`,
   `package.json` (mantido apenas para referência da origem, não
   vendorizado no runtime).
5. Nenhum script de instalação, `postinstall` ou build step executado —
   apenas extração de arquivos já compilados do tarball.
6. Cálculo do SHA-256 do arquivo final vendorizado, registrado acima.

Nenhuma outra dependência externa foi obtida nesta tarefa.
