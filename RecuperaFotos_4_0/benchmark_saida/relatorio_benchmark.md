# Recupera Fotos 4.0 — benchmark real em imagem de 2048.0 MiB

*Gerado por `python benchmark.py--reusar` em 11/11/2026 09:22:26. Todos os números foram medidos nesta execução; nenhum é estimado.*

## Ambiente

- sistema: Linux 6.1.158+ (x86_64) | cpus: 2
- python: 3.11.2
- Pillow: 13.0.0
- imagem: `/tmp/rf4_bench/cartao_benchmark.img` — 2147483648 bytes (2.000 GiB), arquivo real em disco, fora do repositório
- construção da imagem: 22.2s
- volume FAT32: LBA 2048 (1048576 B), 512.0 MiB, 16381 clusters de 32 KiB
- espaço livre dentro do volume (lendo a FAT): 15643 clusters = 488.8 MiB — é isso o que o cenário A varre
- área não alocada: 537919488 .. 2147483648 (1535.0 MiB) de dados aleatórios com as assinaturas de imagem neutralizadas

## Gabarito (o que existe na imagem, conferido por SHA-256)

- 45 fotos vivas no FAT32 (não devem ser recuperadas pela etapa 1; o carving de cartão inteiro as reencontra, porque elas ainda estão lá)
- 30 fotos apagadas no FAT32 (1 fragmentada), 6.4 MiB
- 50 fotos órfãs na área não alocada, 12.6 MiB — só a etapa 2 (carving) as alcança
- tamanho médio de uma foto: 264783 B

## Leitura bruta (teto de I/O do ambiente)

- 2147483648 bytes lidos em 0.4s = **5198 MB/s** (blocos de 4 MiB, erros de leitura: 0)
- atenção: a imagem acabou de ser gravada, então o Linux a serve do cache de página (o sandbox não permite descartar o cache com `echo 3 > /proc/sys/vm/drop_caches`). Este número é o teto de leitura+CPU do ambiente, não a velocidade de um cartão físico.

## Resultados

| cenário | varrido | tempo total | etapa 1 | etapa 2 | MB/s | gravados | idênticos ao alvo | vivos reencontrados | fora do gabarito | apagadas | órfãs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A — padrão da interface (só espaço livre) | 488.8 MiB | 3.9s | 0.2s | 3.7s | 124 | 30 | 30 | 0 | 0 | 30/30 | 0/50 |
| B — cartão inteiro com verificação | 2048.0 MiB | 23.2s | 0.2s | 23.1s | 88 | 125 | 80 | 45 | 0 | 30/30 | 50/50 |
| C — cartão inteiro sem verificação | 2048.0 MiB | 22.2s | 0.0s | 22.2s | 92 | 125 | 80 | 45 | 0 | 30/30 | 50/50 |

### A — padrão da interface (só espaço livre)

- opções: `{"apenas_espaco_livre": true, "verificar": true}`
- o que foi varrido: só os clusters livres do volume (488.8 MiB)
- tempo: 3.9s total (etapa 1 0.2s + etapa 2 3.7s) | 488.8 MiB varridos | 124 MB/s
- cpu: 3.8s usuário + 0.1s sistema | memória durante o cenário (RSS): 25 MB | pico do processo inteiro: 42 MB
- recuperados: 30 | candidatos em memória: 30 | rejeitados: 0 | recusas NTFS: 0
- gravados no destino: 30 arquivo(s), 6.4 MiB | volumes: `['fat32@1048576']`
- por estado: `{"ok": 30, "nao_verificado": 0, "corrompido": 0, "parcial": 0}`
- por método: `{"pasta": 30}`
- duplicados descartados: 0 por hash, 0 por região sobreposta
- erros de leitura: 0 | sistemas detectados: `['fat32']` | cancelado: False
- conferência SHA-256: 30 gravado(s) idêntico(s) ao alvo (30 foto(s) distinta(s)); 0 foto(s) viva(s) reencontrada(s) pelo carving (0 distinta(s)); 0 fora do gabarito
- relatórios CSV no destino (não contam como recuperação): 1
- recall: apagadas 30/30 | órfãs 0/50
- avisos: origem é um arquivo de imagem; gravação no mesmo disco autorizada
- registro da execução:
    - +0.001s — Etapa 1: recuperação por sistema de arquivos (origem: /tmp/rf4_bench/cartao_benchmark.img)
    - +0.001s — Sistema de arquivos identificado: fat32
    - +0.024s — RECUPERADO IMG_2000.JPG (222650 bytes, pasta, estado=ok)
    - +0.031s — RECUPERADO IMG_2001.JPG (222623 bytes, pasta, estado=ok)
    - +0.037s — RECUPERADO IMG_2002.JPG (222591 bytes, pasta, estado=ok)
    - +0.043s — RECUPERADO IMG_2003.JPG (222762 bytes, pasta, estado=ok)
    - +0.049s — RECUPERADO IMG_2004.JPG (222675 bytes, pasta, estado=ok)
    - +0.056s — RECUPERADO IMG_2005.JPG (222657 bytes, pasta, estado=ok)
    - +0.062s — RECUPERADO IMG_2006.JPG (222964 bytes, pasta, estado=ok)
    - +0.068s — RECUPERADO IMG_2007.JPG (222787 bytes, pasta, estado=ok)
    - +0.074s — RECUPERADO IMG_2008.JPG (222837 bytes, pasta, estado=ok)
    - +0.08s — RECUPERADO IMG_2009.JPG (222852 bytes, pasta, estado=ok)
    - +0.086s — RECUPERADO IMG_2010.JPG (222384 bytes, pasta, estado=ok)
    - +0.092s — RECUPERADO IMG_2011.JPG (222611 bytes, pasta, estado=ok)
    - +0.098s — RECUPERADO IMG_2012.JPG (222656 bytes, pasta, estado=ok)
    - +0.104s — RECUPERADO IMG_2013.JPG (222546 bytes, pasta, estado=ok)

### B — cartão inteiro com verificação

- opções: `{"cartao_inteiro": true, "apenas_espaco_livre": false, "verificar": true}`
- o que foi varrido: o cartão inteiro (2048.0 MiB)
- tempo: 23.2s total (etapa 1 0.2s + etapa 2 23.1s) | 2048.0 MiB varridos | 88 MB/s
- cpu: 22.4s usuário + 0.8s sistema | memória durante o cenário (RSS): 26 MB | pico do processo inteiro: 53 MB
- recuperados: 125 | candidatos em memória: 125 | rejeitados: 0 | recusas NTFS: 0
- gravados no destino: 125 arquivo(s), 29.5 MiB | volumes: `['fat32@1048576']`
- por estado: `{"ok": 125, "nao_verificado": 0, "corrompido": 0, "parcial": 0}`
- por método: `{"pasta": 30, "varredura": 95}`
- duplicados descartados: 0 por hash, 0 por região sobreposta
- erros de leitura: 0 | sistemas detectados: `['fat32']` | cancelado: False
- conferência SHA-256: 80 gravado(s) idêntico(s) ao alvo (80 foto(s) distinta(s)); 45 foto(s) viva(s) reencontrada(s) pelo carving (45 distinta(s)); 0 fora do gabarito
- relatórios CSV no destino (não contam como recuperação): 1
- recall: apagadas 30/30 | órfãs 50/50
- avisos: origem é um arquivo de imagem; gravação no mesmo disco autorizada
- registro da execução:
    - +0.0s — Etapa 1: recuperação por sistema de arquivos (origem: /tmp/rf4_bench/cartao_benchmark.img)
    - +0.001s — Sistema de arquivos identificado: fat32
    - +0.007s — RECUPERADO IMG_2000.JPG (222650 bytes, pasta, estado=ok)
    - +0.012s — RECUPERADO IMG_2001.JPG (222623 bytes, pasta, estado=ok)
    - +0.017s — RECUPERADO IMG_2002.JPG (222591 bytes, pasta, estado=ok)
    - +0.022s — RECUPERADO IMG_2003.JPG (222762 bytes, pasta, estado=ok)
    - +0.027s — RECUPERADO IMG_2004.JPG (222675 bytes, pasta, estado=ok)
    - +0.032s — RECUPERADO IMG_2005.JPG (222657 bytes, pasta, estado=ok)
    - +0.037s — RECUPERADO IMG_2006.JPG (222964 bytes, pasta, estado=ok)
    - +0.042s — RECUPERADO IMG_2007.JPG (222787 bytes, pasta, estado=ok)
    - +0.047s — RECUPERADO IMG_2008.JPG (222837 bytes, pasta, estado=ok)
    - +0.052s — RECUPERADO IMG_2009.JPG (222852 bytes, pasta, estado=ok)
    - +0.058s — RECUPERADO IMG_2010.JPG (222384 bytes, pasta, estado=ok)
    - +0.063s — RECUPERADO IMG_2011.JPG (222611 bytes, pasta, estado=ok)
    - +0.067s — RECUPERADO IMG_2012.JPG (222656 bytes, pasta, estado=ok)
    - +0.072s — RECUPERADO IMG_2013.JPG (222546 bytes, pasta, estado=ok)

### C — cartão inteiro sem verificação

- opções: `{"cartao_inteiro": true, "apenas_espaco_livre": false, "verificar": false, "sem_verificar": true}`
- o que foi varrido: o cartão inteiro (2048.0 MiB)
- tempo: 22.2s total (etapa 1 0.0s + etapa 2 22.2s) | 2048.0 MiB varridos | 92 MB/s
- cpu: 21.5s usuário + 0.7s sistema | memória durante o cenário (RSS): 26 MB | pico do processo inteiro: 53 MB
- recuperados: 125 | candidatos em memória: 125 | rejeitados: 0 | recusas NTFS: 0
- gravados no destino: 125 arquivo(s), 29.5 MiB | volumes: `['fat32@1048576']`
- por estado: `{"ok": 0, "nao_verificado": 125, "corrompido": 0, "parcial": 0}`
- por método: `{"pasta": 30, "varredura": 95}`
- duplicados descartados: 0 por hash, 0 por região sobreposta
- erros de leitura: 0 | sistemas detectados: `['fat32']` | cancelado: False
- conferência SHA-256: 80 gravado(s) idêntico(s) ao alvo (80 foto(s) distinta(s)); 45 foto(s) viva(s) reencontrada(s) pelo carving (45 distinta(s)); 0 fora do gabarito
- relatórios CSV no destino (não contam como recuperação): 1
- recall: apagadas 30/30 | órfãs 50/50
- avisos: origem é um arquivo de imagem; gravação no mesmo disco autorizada
- registro da execução:
    - +0.0s — Etapa 1: recuperação por sistema de arquivos (origem: /tmp/rf4_bench/cartao_benchmark.img)
    - +0.001s — Sistema de arquivos identificado: fat32
    - +0.002s — RECUPERADO IMG_2000.JPG (222650 bytes, pasta, estado=nao_verificado)
    - +0.002s — RECUPERADO IMG_2001.JPG (222623 bytes, pasta, estado=nao_verificado)
    - +0.003s — RECUPERADO IMG_2002.JPG (222591 bytes, pasta, estado=nao_verificado)
    - +0.003s — RECUPERADO IMG_2003.JPG (222762 bytes, pasta, estado=nao_verificado)
    - +0.004s — RECUPERADO IMG_2004.JPG (222675 bytes, pasta, estado=nao_verificado)
    - +0.004s — RECUPERADO IMG_2005.JPG (222657 bytes, pasta, estado=nao_verificado)
    - +0.005s — RECUPERADO IMG_2006.JPG (222964 bytes, pasta, estado=nao_verificado)
    - +0.005s — RECUPERADO IMG_2007.JPG (222787 bytes, pasta, estado=nao_verificado)
    - +0.006s — RECUPERADO IMG_2008.JPG (222837 bytes, pasta, estado=nao_verificado)
    - +0.006s — RECUPERADO IMG_2009.JPG (222852 bytes, pasta, estado=nao_verificado)
    - +0.007s — RECUPERADO IMG_2010.JPG (222384 bytes, pasta, estado=nao_verificado)
    - +0.007s — RECUPERADO IMG_2011.JPG (222611 bytes, pasta, estado=nao_verificado)
    - +0.008s — RECUPERADO IMG_2012.JPG (222656 bytes, pasta, estado=nao_verificado)
    - +0.008s — RECUPERADO IMG_2013.JPG (222546 bytes, pasta, estado=nao_verificado)

- **memória durante o cenário** (VmRSS) é o que o processo ocupa enquanto varre o cartão. O **pico do processo inteiro** (VmHWM) é o maior valor já atingido desde o início do processo: numa execução que também constrói a imagem (`python benchmark.py` sem `--reusar`) esse pico passa de 1 GB, porque o volume FAT32 de 512 MiB é montado em memória antes de ser gravado — isso é custo do gerador de teste, não do recuperador. Com `--reusar`, o pico mede só a recuperação.

## Como interpretar

- **MB/s efetivo** = bytes varridos no cenário ÷ tempo total. O teto de leitura bruta medido acima é o limite físico do ambiente: tudo o que fica abaixo dele é análise de assinaturas, verificação estrutural, Pillow e gravação.
- **alvo** = as fotos apagadas do FAT32 + as órfãs da área não alocada. **fora do gabarito** = arquivo gravado cujo SHA-256 não corresponde a nenhuma foto plantada (nem viva, nem apagada, nem órfã): inclui pedaços parciais de fotos reais (estado `parcial`), que por definição não são byte a byte iguais ao original, e achados do acaso em dados aleatórios — o CSV de cada cenário diz o estado e o motivo de cada um.
- **vivos reencontrados** só aparece no modo cartão inteiro: sem a restrição de espaço livre o carving também devolve as fotos que nunca foram apagadas. É o comportamento esperado dessa opção.
- Com `apenas_espaco_livre` a etapa 2 varre somente os clusters livres do volume (contados lendo a FAT da imagem), e a etapa 1 só devolve entradas apagadas — por isso as fotos vivas não aparecem no cenário A.
- O cenário B encontra as órfãs da área não alocada; o A não as procura (por projeto: só o espaço livre do volume reconhecido). Isso é a diferença entre as duas opções na interface.
