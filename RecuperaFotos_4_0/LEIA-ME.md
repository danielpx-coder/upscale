# Recupera Fotos 4.0

Recupera fotos e vídeos apagados de cartões de memória, pendrives, HDs, SSDs,
dispositivos `\\.\PhysicalDriveN` (Windows), `/dev/sdX` (Linux), imagens de
disco e pastas comuns.

O programa trabalha em **duas etapas**:

1. **Etapa 1 — metadados do sistema de arquivos.** Lê as estruturas reais do
   FAT16, FAT32, exFAT ou NTFS (entradas de diretório apagadas, MFT, cadeias da
   FAT, *data runs*, *attribute lists*) e reconstitui o arquivo com **nome,
   pasta original, data e tamanho verdadeiros**. É o caminho que devolve o
   arquivo inteiro e intacto.
2. **Etapa 2 — varredura por assinatura (*file carving*).** Percorre o espaço
   livre procurando o começo de cada arquivo pelos bytes que o identificam
   (JPEG, PNG, TIFF/CR2/NEF/ARW/DNG/ORF/RW2, HEIC/AVIF, GIF, BMP, WebP, PSD,
   RAF, MP4/MOV/M4V/3GP, MKV, AVI, MPEG-TS) e mede o comprimento real de cada
   um. Os arquivos saem com nome `carv_<posição>.<ext>` (com data na frente,
   quando a data é conhecida).

A origem é aberta **sempre somente para leitura** — o programa não grava, não
"repara" e não altera um byte do cartão.

---

## 1. Como executar

### Windows (sem instalar nada)

Dê dois cliques em **`Iniciar.bat`**.

O script procura um Python 3 (9 ou mais novo), confere o Pillow e abre a
janela. Se não houver Python, ele mostra de onde baixar e o que marcar no
instalador ("Add python.exe to PATH").

Para gerar um `.exe` portátil: dois cliques em **`Gerar_EXE.bat`** (instala o
PyInstaller e produz `dist\RecuperaFotos\RecuperaFotos.exe`; copie a pasta
inteira para onde quiser).

Para conferir o programa: **`Executar_Testes.bat`** roda os 431 testes e grava
`resultado_testes.txt`.

### Qualquer sistema (Linux, macOS, Windows)

```bash
python3 -m pip install --user -r requirements.txt   # opcional (Pillow)
python3 recupera_fotos.py                            # abre a janela
```

> **Pillow é opcional.** Sem ele o programa avisa na tela e continua
> funcionando: todos os verificadores de integridade são próprios, escritos
> sobre os bytes do arquivo. Com o Pillow, além da verificação estrutural, a
> imagem é **decodificada de verdade**, o que fortalece o estado `ok`.

---

## 2. A janela, passo a passo

A janela tem quatro cartões numerados à esquerda e duas abas à direita
("Fotos recuperadas" e "Registro").

**1. Cartão, pendrive, HD ou pasta**
A lista mostra os dispositivos detectados com capacidade, se são removíveis e
as letras de unidade. Ao lado, o programa avisa quando um item é **o disco do
sistema operacional** — nesse caso ele só aceita a leitura e recomenda criar
uma imagem antes. Botões: *Atualizar lista*, *Arquivo de imagem…* e *Escolher
pasta de origem…*.

**2. Onde salvar**
A pasta de destino **precisa estar em outro disco físico**. Se você apontar
para o mesmo disco, o programa explica por que isso é perigoso (gravar em cima
dos dados que ainda poderiam ser recuperados) e não inicia. Só existe uma
exceção, autorizada explicitamente: quando a origem é um **arquivo de imagem**
(uma cópia), não a mídia original.

**3. Período**
*Todas as datas*, *Hoje*, *Ontem e hoje*, *Últimos 7 dias*, *Últimos 30 dias*
ou *Data específica* (DD/MM/AAAA). A marcação *Considerar arquivos sem data
conhecida* mantém os arquivos cuja data não pôde ser lida — sem ela, esses
arquivos são descartados.

**4. Opções**

| opção | o que faz |
|---|---|
| Recuperar também vídeos | inclui MOV/MP4/M4V/3GP/MKV/AVI/M2TS (por padrão só fotos, mas HEIC/AVIF/CR3 continuam inclusos porque são fotografias) |
| Buscar assinaturas apenas no espaço livre | a etapa 2 ignora os clusters ocupados. **Marcado por padrão** — é mais rápido e evita duplicatas |
| Conferir as imagens e separar as corrompidas | classifica cada arquivo em `ok`, `parcial`, `corrompido` ou `nao_verificado`. **Marcado por padrão** |
| Busca profunda | varre também metadados órfãos (ex.: registros MFT fora da MFT principal) |
| Incluir arquivos menores de 8 KB | por padrão o programa ignora arquivos com menos de 8 KB (miniaturas e ícones) |
| Pausar depois da primeira etapa | mostra o resultado da etapa 1 e espera você decidir entre *Continuar (etapa 2)* ou *Finalizar* |
| Buscar partições perdidas | procura assinaturas de boot de partições que a tabela de partições perdeu |

**Botões.** *Iniciar recuperação* / *Cancelar* (para com segurança, no fim do
bloco corrente) / *Continuar (etapa 2)* e *Finalizar* (aparecem só quando você
pediu a pausa).

**Aba "Fotos recuperadas".** Miniaturas em 4 colunas (até 200 por vez, para a
janela não travar), com nome, tamanho e estado. Dois cliques abre a imagem
ampliada. O filtro por estado (*todos / ok / parcial / corrompido /
nao_verificado*) reexibe a galeria sem reler o cartão. Abaixo, *Abrir pasta de
destino* e *Abrir diagnóstico NTFS*.

**Aba "Registro".** Tudo o que o programa fez, com hora: sistema de arquivos
identificado, cada arquivo recuperado, avisos e erros. A lista se auto-limita
em 4.000 linhas (as mais novas ficam).

---

## 3. O que você recebe no destino

```
E:\Recuperadas\
├── DCIM\100CANON\IMG_2031.JPG      ← estrutura original, quando conhecida
├── 20240615_103020_carv_00086016.jpg  ← varredura, com a data na frente
├── _corrompidas\                   ← o que falhou na verificação
│   └── carv_00123456.jpg
├── relatorio_recuperacao.csv       ← planilha com todos os arquivos
└── diagnostico_ntfs.csv            ← só quando há NTFS com registros recusados
```

**Os quatro estados** (o programa nunca mente sobre o que encontrou):

| estado | significado |
|---|---|
| `ok` | estrutura interna completa e coerente — e, com Pillow, a imagem decodifica |
| `parcial` | o começo é legível mas o arquivo foi interrompido (sobrescrito ou truncado). O relatório diz quantos bytes são aproveitáveis |
| `corrompido` | a estrutura não fecha. Vai para `_corrompidas\` — alguns programas ainda conseguem abrir |
| `nao_verificado` | a verificação estava desativada, ou não há verificador para aquele formato |

**`relatorio_recuperacao.csv`** abre direto no Excel (UTF-8 com BOM,
separador `;`) e traz: `arquivo`, `tipo`, `encontrado_por`, `caminho_original`,
`posicao_no_cartao`, `tamanho_bytes`, `data`, `estado`, `fracao_recuperada`,
`metodo`, `motivo`. A coluna **`motivo`** é a mais importante: ela explica, em
português, por que o arquivo recebeu aquele estado (ex.: *"EOI não encontrado
— 5358 byte(s) de imagem aproveitáveis"*, *"clusters da cadeia realocados
(ex.: 3)"*, *"data runs fora do volume"*).

Duplicatas são evitadas duas vezes: pelo SHA-256 do conteúdo e pela região
física do disco.

---

## 4. Linha de comando

```bash
python recupera_fotos.py --listar --todos-os-discos
python recupera_fotos.py F: -o E:\Recuperadas
python recupera_fotos.py disco2 -o E:\Recuperadas --videos --profunda
python recupera_fotos.py disco2 -o E:\Recuperadas --buscar-particoes
python recupera_fotos.py F: -o E:\Recuperadas --desde 7d
python recupera_fotos.py disco2 --criar-imagem E:\origem.img --retomar-imagem
python recupera_fotos.py E:\origem.img -o E:\Recuperadas --min-kb 0
python recupera_fotos.py --help
```

| opção | efeito |
|---|---|
| `-o`, `--destino` | pasta em **outro disco físico** (obrigatória) |
| `--listar`, `--todos-os-discos` | lista os dispositivos e sai |
| `--videos` | recupera vídeos também |
| `--buscar-particoes` | procura partições perdidas |
| `--criar-imagem CAMINHO` | cria uma imagem forense da origem e sai |
| `--retomar-imagem` | retoma uma aquisição interrompida (revalida blocos) |
| `--pasta-relativa PASTA` | só arquivos cuja pasta original bata com PASTA |
| `--profunda` | busca profunda de metadados órfãos |
| `--min-kb N` | tamanho mínimo (`0` = sem mínimo; padrão: 8) |
| `--desde` | `hoje`, `ontem`, `Nd` (ex. `7d`) ou `DD/MM/AAAA` |
| `--ignorar-sem-data` | descarta arquivos sem data conhecida |
| `--sem-pastas` | não recria a estrutura de pastas |
| `--sem-verificar` | não classifica integridade (tudo vira `nao_verificado`) |
| `--sem-data-no-nome` | não prefixa a data nos arquivos de varredura |
| `--cartao-inteiro` / `--unidade-inteira` | carving em todo o volume |
| `--volume-todo` / `--espaco-livre` | controlam o alcance da etapa 2 |
| `--pausar` | encerra depois da etapa 1 (na CLI, imprime o aviso) |
| `--permitir-mesmo-disco` | autoriza destino no mesmo disco **apenas** quando a origem é um arquivo de imagem |
| `--limite N` | para depois de N arquivos |

**Códigos de saída:** `0` sucesso · `1` falha ao carregar a interface ·
`2` parâmetro inválido (destino ausente, data inválida, `--min-kb` negativo,
`--retomar-imagem` sem `--criar-imagem`, destino no mesmo disco físico, imagem
que já existe).

Num `.exe` gerado sem console, a saída da CLI vai para
`recupera_fotos_log.txt`, ao lado do executável, e o caminho é informado no
fim.

---

## 5. Imagem forense: quando vale a pena

Se o cartão está fisicamente danificado, faz leituras lentas ou você quer
tentar várias estratégias sem tocar na mídia, crie primeiro uma imagem:

```bash
python recupera_fotos.py F: --criar-imagem E:\cartao.img
python recupera_fotos.py E:\cartao.img -o E:\Recuperadas
```

A aquisição lê em blocos de 4 MiB, registra o resultado de cada bloco em
`cartao.img.map.jsonl` (versão 1) e **nunca inventa dados**: setor ilegível é
preenchido com `0xF8` e marcado no mapa, não zerado. Interrompeu? Repita o
comando com `--retomar-imagem` — os blocos já gravados são revalidados pelo
hash e a cópia continua de onde parou.

---

## 6. O que este programa NÃO faz (e nenhum outro faz)

- **Não recupera o que foi sobrescrito.** Se o espaço do arquivo já foi usado
  por outro arquivo, os bytes antigos deixaram de existir. A etapa 2 devolve o
  que ainda está lá, e só isso.
- **Não repara arquivo corrompido.** Ele separa e explica; não reconstrói
  pixels que faltam.
- **Não grava na origem.** Nem "reparo de sistema de arquivos", nem
  desfragmentação, nem escrita de qualquer tipo.
- **Fragmentação é o limite real.** Com a cadeia da FAT/MFT preservada, o
  arquivo fragmentado sai inteiro (o relatório avisa que a cadeia foi
  reconstruída). Sem cadeia, o carving para no primeiro trecho contíguo e o
  arquivo sai como `parcial` — com a fração recuperada medida, nunca estimada.
- **Vídeos fragmentados** (comum em câmeras que gravam MP4 em pedaços)
  raramente se reconstroem por assinatura: o índice `moov` costuma ficar no
  fim do arquivo.
- **Sistemas criptografados** (BitLocker, FileVault, LUKS) só rendem algo se a
  mídia estiver descriptografada no momento da leitura.

Quando o estado sair `parcial` ou `corrompido`, abra o CSV e leia a coluna
`motivo`: ela diz exatamente o que faltou.

---

## 7. Segurança

- A origem é aberta com `open(..., "rb")` (sem `+`) ou com o identificador do
  Windows em modo somente-leitura: **não existe caminho de código que grave na
  mídia de origem**.
- O destino é validado antes de começar: precisa existir (ou poder ser
  criado), ser gravável, ter espaço suficiente e **estar em outro disco
  físico**. O mesmo disco só é aceito com autorização explícita e apenas quando
  a origem é uma imagem.
- Nomes de arquivo vindos do cartão são higienizados: `..\..\evil.jpg` vira
  `_.._evil.jpg`, e todo caminho final é conferido com `realpath` para
  garantir que ficou **dentro** da pasta de destino.
- Nenhuma leitura única passa de 8 MiB, correntes de clusters têm limite e a
  cadeia de EBRs para em 128 entradas: um cartão danificado não derruba o
  programa nem consome toda a memória.
- Leitura além do fim físico **não** é preenchida com zeros silenciosamente:
  o que não pôde ser lido é declarado.

---

## 8. Arquivos deste pacote

| arquivo | o que é |
|---|---|
| `recupera_fotos.py` | ponto de entrada (janela + CLI) |
| `interface.py` | a janela (Tkinter) |
| `motor.py` | o orquestrador: etapas, filtros, nomes, relatórios |
| `aquisicao.py` | leitores (arquivo, dispositivo, recorte), imagem forense, validação de destino |
| `particoes.py` | MBR, GPT, EBR, boot de reserva, partições perdidas |
| `fatfs.py` / `exfat.py` / `ntfs.py` | os três sistemas de arquivos |
| `assinaturas.py` | carving: 14 assinaturas de início de arquivo e 11 analisadores de formato |
| `verificacao.py` | classificação de integridade (estrutural + Pillow) e EXIF |
| `candidatos.py` | o modelo de arquivo candidato e seus estados |
| `rodar_testes.py` | executa a suíte e grava `resultado_testes.txt` |
| `benchmark.py` | constrói uma imagem de 2 GiB com gabarito e mede desempenho e acerto |
| `tests/` | 431 testes (inclui um Tk sem tela, para testar a janela em qualquer sistema) |
| `Iniciar.bat`, `Executar_Testes.bat`, `Gerar_EXE.bat` | atalhos do Windows |
| `RecuperaFotos.spec`, `requirements.txt` | empacotamento |
| `resultado_testes.txt`, `benchmark_saida/` | **saídas reais**, geradas pelos comandos acima |
| `LEIA-ME.md`, `VALIDACAO.md` | este manual e o relatório de validação |

Para reproduzir tudo:

```bash
python rodar_testes.py     # -> resultado_testes.txt
python benchmark.py        # -> benchmark_saida/relatorio_benchmark.md
```

Os dois arquivos de resultado são **gerados**, nunca escritos à mão.
