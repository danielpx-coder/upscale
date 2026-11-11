# Recupera Fotos 4.0 — relatório de validação

Este documento descreve **como** o programa foi validado, **o que** foi medido e
**o que não foi** validado. Todos os números citados aqui foram produzidos por
comandos executados de verdade e estão gravados nos arquivos indicados — nada é
estimativa nem arredondamento simpático.

Data da validação: **11/11/2026**.
Ambiente: Linux 6.1.158+ (x86_64), 2 CPUs, ~3,9 GB de RAM, Python 3.11.2,
Pillow 13.0.0. **Sem Windows e sem mídia física** neste ambiente — veja a
seção 6.

---

## 1. Como reproduzir

```bash
python rodar_testes.py     # -> resultado_testes.txt   (saída real do unittest)
python benchmark.py        # -> benchmark_saida/relatorio_benchmark.md
                           #    benchmark_saida/medidas.json
                           #    benchmark_saida/gabarito.json
                           #    benchmark_saida/relatorio_cenario_{0,1,2}.csv
```

`benchmark.py` constrói uma imagem de **2 GiB** em `/tmp/rf4_bench/` (fora do
repositório), com gabarito conhecido, e a apaga quando você quiser.
`python benchmark.py --rapido` faz o mesmo com 512 MiB, para conferir o script.

---

## 2. Testes automatizados — resultado real

Arquivo: **`resultado_testes.txt`** (gerado, 52 KB, com o nome e o resultado de
cada um dos 428 testes).

```
módulo                    exec.    ok  falhas  erros  pulos
test_advanced_ntfs           34    34       0      0      0
test_aquisicao               44    43       0      0      1
test_fuzz                    37    37       0      0      0
test_interface               61    61       0      0      0
test_ntfs                    40    40       0      0      0
test_recupera_fotos         134   134       0      0      0
test_regressions             78    78       0      0      0
------------------------------------------------------------------------
TOTAL                       428   427       0      0      1

RESULTADO FINAL: 428 teste(s) executado(s) em 33.2s
  aprovados : 427     reprovados: 0     pulados: 1
  STATUS    : TODOS OS TESTES PASSARAM
```

O único pulo é `test_aquisicao.test_letra_unidade_windows` ("letras de
unidade: só Windows") — exige o registro do Windows e não tem como rodar em
Linux. Não há teste pulado por conveniência.

### O que cada módulo cobre

| módulo | foco |
|---|---|
| `test_aquisicao` | leitores de arquivo/dispositivo/recorte, leitura além do fim, contagem de erros, `listar_dispositivos`, `destino_mesmo_disco_fisico`, `validar_destino`, `Intervalos`, `Adquiridor` (blocos de 4 MiB, SHA-256, `.map.jsonl` v1, retomada, `0xF8` em setor ilegível) |
| `test_ntfs` | fixup/USA, `RunList`, LZNT1, `RegistroMFT`, `SistemaNTFS.recuperar` e `recuperar_mft_orfa` (que devolvem `(candidatos, recusas)`) |
| `test_advanced_ntfs` | ATTRIBUTE_LIST com extents em registros distintos, atributos comprimidos, boot de reserva do NTFS, MFT órfã, confiabilidade |
| `test_regressions` | **78 travas de regressão**: cada uma existe porque um defeito real foi encontrado e corrigido (lista na seção 4) |
| `test_fuzz` | 37 testes com entradas hostis: run lists assimétricas/sparse/degeneradas/truncadas, correntes de FAT cíclicas, EBR auto-referentes, GPT com entradas sobrepostas, nomes com `..\..\`, imagens só de zeros e só de `0xFF` |
| `test_recupera_fotos` | 134 testes de integração: FAT16/FAT32/exFAT/NTFS montados de verdade, formatação rápida, MBR/GPT/EBR/partição perdida, carving em 20+ formatos, deduplicação, CSV, filtros de data e de tamanho, e a CLI (saída e códigos de saída) |
| `test_interface` | 61 testes da **janela**, executados com um Tk sem tela (`tests/tk_stub.py`), inclusive um fluxo completo de recuperação |

### A janela é testada de verdade

O ambiente não tem `tkinter` instalado, então `tests/tk_stub.py` (~960 linhas)
implementa um duplo do Tk: widgets, variáveis, `after`, `filedialog`,
`messagebox`, `PIL.ImageTk`. Os testes importam `interface.py` contra esse
duplo e exercitam a interface real — não uma cópia dela. Entre as 61
verificações:

- título "Recupera Fotos 4.0", geometria 1240x780, mínimo 1080x660;
- os quatro cartões ("1. Cartão, pendrive, HD ou pasta", "2. Onde salvar",
  "3. Período", "4. Opções"), as duas abas, os 7 checkboxes de opção e os 6
  botões de período existem e têm os textos certos;
- `_opcoes_da_gui()` traduz corretamente a tela em `motor.Opcoes` (inclusive:
  **nenhum controle da janela consegue autorizar destino no mesmo disco**);
- fluxo completo com imagem FAT32 real: 3 fotos recuperadas, barra em 100%,
  contadores, galeria com 3 células, arquivos + CSV no disco;
- pausa após a etapa 1 → *Continuar* e → *Finalizar* (com a frase de status
  correta: "finalizada após a etapa 1 (varredura não executada)");
- *Cancelar* → `res.cancelado` e status "cancelada";
- clique duplo em *Iniciar* não abre duas threads; erro do motor → `showerror`
  e botão reabilitado;
- **segurança de thread**: 4 threads × 100 chamadas de progresso/log não tocam
  em nenhum widget (os callbacks só enfileiram; a tela é atualizada pelo
  `after` de 100 ms), a ordem FIFO se mantém e o registro se auto-limita;
- galeria: teto de 200 miniaturas, filtro por estado, ausência de miniatura
  para vídeo, nome longo com reticências, duplo clique abrindo a imagem;
- lista de dispositivos: formatação com capacidade/"removível"/"DISCO DO
  SISTEMA", lista vazia, falha ao listar, alerta de disco do sistema;
- diálogos de arquivo/pasta, *Abrir pasta de destino*, *Abrir diagnóstico NTFS*
  e a falha do `Popen` (com `showwarning`).

---

## 3. Desempenho e acerto em 2 GiB — resultado real

Arquivo: **`benchmark_saida/relatorio_benchmark.md`** (+ `medidas.json`).

**A imagem construída** (gabarito em `benchmark_saida/gabarito.json`, com o
SHA-256 de cada foto):

| região | conteúdo |
|---|---|
| setor 0 | MBR com uma partição FAT32 (LBA 2048) |
| 1 MiB – 513 MiB | volume FAT32 real, 16.381 clusters de 32 KiB, com **45 fotos vivas** e **30 apagadas** (a última fragmentada) em `DCIM/100CANON` |
| 513 MiB – 2 GiB | **1.535 MiB de espaço não alocado**: dados aleatórios com todas as assinaturas de imagem neutralizadas, e **50 JPEGs órfãos** encaixados em offsets conhecidos |

Espaço livre dentro do volume, medido lendo a FAT: **15.643 clusters =
488,8 MiB** (é o que o cenário A varre).

**Leitura bruta:** 2.147.483.648 bytes em 0,4 s = **5.198 MB/s**, 0 erros.
*Atenção:* a imagem acabara de ser gravada, então o Linux a serviu do cache de
página (o sandbox não permite `drop_caches`). É o teto de leitura+CPU do
ambiente, **não** a velocidade de um cartão físico.

**Cenários:**

| cenário | varrido | tempo | etapa 1 | etapa 2 | MB/s | gravados | idênticos ao alvo | vivos reencontrados | fora do gabarito | apagadas | órfãs |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A — padrão da interface (só espaço livre) | 488,8 MiB | 3,9 s | 0,2 s | 3,7 s | 124 | 30 | 30 | 0 | 0 | **30/30** | 0/50 |
| B — cartão inteiro com verificação | 2.048 MiB | 23,2 s | 0,2 s | 23,1 s | 88 | 125 | 80 | 45 | 0 | **30/30** | **50/50** |
| C — cartão inteiro sem verificação | 2.048 MiB | 22,2 s | 0,0 s | 22,2 s | 92 | 125 | 80 | 45 | 0 | **30/30** | **50/50** |

*(memória e CPU do artefato: 25–26 MB de RSS durante a varredura, pico de
42–53 MB no processo; 3,8 s de CPU usuário no cenário A e 22,4 s no B.)*

Leituras que interessam:

- **Acerto: 100% do que existe e é alcançável.** As 30 fotos apagadas
  (inclusive a fragmentada, reconstruída seguindo a cadeia da FAT que
  sobreviveu) e as 50 órfãs da área não alocada saíram **byte a byte idênticas**
  ao original, conferidas por SHA-256 — não por tamanho nem por extensão.
- **Zero falso positivo.** Nenhum arquivo gravado ficou fora do gabarito nos
  três cenários, com 1.535 MiB de dados aleatórios varridos. As sequências
  `FFD8FF` que o acaso cria foram neutralizadas na construção, mas as `BM`
  (2 bytes, ~23 mil ocorrências aleatórias em 1,5 GiB) foram deixadas de
  propósito, porque existem em cartões de verdade: o analisador BMP as recusou
  todas.
- **Os 45 "vivos reencontrados" do cenário B não são erro**: sem a restrição de
  espaço livre o carving devolve também as fotos que nunca foram apagadas (elas
  estão lá). É o comportamento esperado do "cartão inteiro", e está explicado
  no relatório.
- **Custo da verificação: ~1,0 s em 23 s (≈4%)** — diferença B×C. Barata perto
  do que ela entrega (separar `ok` de `parcial`/`corrompido`). No cenário C
  todos os 125 saem como `nao_verificado`, como prometido.
- **Memória: 26 MB de RSS varrendo 2 GiB** (pico do processo: 53 MB). O
  programa não carrega a imagem na memória: lê em blocos de no máximo 8 MiB.
  Esse pico só passa de 1 GB quando a **construção** da imagem acontece na
  mesma execução — o gerador de teste monta o volume FAT32 de 512 MiB em
  memória antes de gravá-lo; é custo do `benchmark.py`, não do recuperador.
- **Vazão real: ~88–124 MB/s** neste sandbox, com 14 assinaturas (11
  analisadores de formato) rodando em Python puro sobre cada bloco de 8 MiB. Num cartão físico o
  limite passa a ser a mídia (USB 2.0 ≈ 20 MB/s), não o programa.
- **Duplicatas: 0** por hash e 0 por região nos três cenários — a etapa 2
  respeita as regiões já recuperadas pela etapa 1.
- **Erros de leitura: 0** e origem não modificada (travado por
  `test_origem_nao_e_modificada`, que compara o SHA-256 da imagem antes e
  depois de uma recuperação completa).

Os relatórios CSV dos três cenários estão copiados em
`benchmark_saida/relatorio_cenario_{0,1,2}.csv` — dá para abrir no Excel e
conferir arquivo por arquivo (posição no cartão, estado, fração recuperada e
motivo).
### Reprodutibilidade

O benchmark foi executado quatro vezes neste ambiente (duas construindo a
imagem, duas reaproveitando-a). Em **todas** o acerto foi idêntico: 30/30
apagadas, 50/50 órfãs, 80 arquivos byte a byte idênticos ao gabarito, 0 fora do
gabarito, 0 duplicata, 0 erro de leitura e 45 vivos reencontrados no modo
cartão inteiro. Só o tempo variou, com a carga da máquina:

| execução | A | B | C |
|---|---|---|---|
| 1ª (constrói a imagem) | 2,9 s | 21,1 s | 20,5 s |
| 2ª (ambiente novo, constrói) | 3,4 s | 21,4 s | 20,2 s |
| 3ª (`--reusar`) | 3,4 s | 22,8 s | 20,6 s |
| 4ª (`--reusar`; é a que está nos artefatos) | 3,9 s | 23,2 s | 22,2 s |

A suíte de testes também foi reexecutada em um ambiente recém-criado (venv novo,
Pillow reinstalado, `/tmp` vazio, sem `__pycache__`): **428 testes, 427
aprovados, 0 reprovados, 1 pulo** — o mesmo resultado, em 34,0 s e depois
33,2 s, contra 29,8 s da primeira execução. O tempo varia com a carga; a
contagem não.

---

## 4. Defeitos reais encontrados durante a validação

Cada linha abaixo foi um defeito de verdade, descoberto durante o
desenvolvimento, corrigido e **travado por um teste** que falha se ele voltar.
Os nomes de teste citados existem em `tests/`.

| # | defeito | teste que trava |
|---|---|---|
| 1 | *Data runs* do NTFS lidos com a convenção de nibbles invertida (o nibble baixo é o tamanho do **length**, não do offset — MS-FNTFS) | `test_nibble_baixo_e_o_length_e_alto_e_o_offset`, `test_exemplo_documentado_21_16_00_31_20_05`, `test_ida_e_volta_construtor_parser` |
| 2 | *Run* sparse (offset de 0 bytes) tratado como normal, e *run* degenerado (`n=0`) sem consumir o próprio campo de offset | `test_run_sparse_tem_offset_de_zero_bytes`, `test_run_degenerado_n_zero_e_ignorado` |
| 3 | Divisão offset/length dos tokens LZNT1 errada nas posições potência de dois (MS-XCA 2.1) | `test_tabela_de_bits_pos_16_usa_5_de_offset`, `test_tabela_de_bits_pos_2048_usa_12_de_offset`, `test_ida_e_volta_para_padroes_periodicos` |
| 4 | USA/*fixup* do NTFS aplicado com o setor do volume (4096) em vez de 512 fixos | `test_fixup_usa_512_mesmo_em_volume_de_4096`, `test_registro_com_fixup_invalido_e_recusado` |
| 5 | Entradas de ATTRIBUTE_LIST com layout inventado (VCN em 0x0C, referência em 0x14) | `test_layout_da_entrada_de_28_bytes_mais_nome`, `test_dois_extents_em_registros_distintos` |
| 6 | FAT16 confundido com FAT32 por ler FATSz32 no offset 36 (que no FAT16 é a unidade) | `test_fat16_nao_e_confundido_com_fat32` |
| 7 | Setor de boot aceito sem a assinatura `0x55AA` | `test_assinatura_55aa_e_obrigatoria`, `test_lixo_nao_e_sistema_de_arquivos` |
| 8 | Cópias de reserva do boot ignoradas (FAT32 setor 6, exFAT com checksum, NTFS último setor) | `test_fat32_usa_setor_6`, `test_exfat_usa_regiao_de_backup_com_checksum`, `test_ntfs_usa_ultimo_setor`, `test_motor_avisa_quando_usa_copia_de_reserva` |
| 9 | Arquivo apagado **e fragmentado** tratado como contíguo (a cadeia da FAT que sobrevive à exclusão precisa ser seguida) | `test_apagado_fragmentado_usa_a_cadeia_e_nao_a_contiguidade`, `test_exfat_apagado_com_cadeia_sobrevivente` |
| 10 | Arquivo contíguo declarado confiável sem dizer por quê | `test_apagado_contiguo_com_clusters_realocados_nao_e_confiavel`, `test_motivo_da_contiguidade_e_explicito` |
| 11 | GIF sem trailer virando arquivo enorme; MP4 atravessando o fim da região | `test_gif_com_trailer_no_fim_da_regiao`, `test_mp4_nao_ultrapassa_o_fim_da_regiao` |
| 12 | TIFF/CR2/ORF/RW2/DNG medidos sem seguir StripOffsets/TileOffsets | `test_tiff_segue_strips_e_ifds`, `test_tiff_com_strips_ausentes_nao_vira_ok` |
| 13 | JPEG truncado descartado, ou marcado `ok`, ou "engolindo" o resto do cartão | `test_jpeg_truncado_e_parcial_nunca_ok`, `test_jpeg_truncado_nao_engole_o_cartao` |
| 14 | Índice MPF incoerente usado para "completar" o arquivo | `test_indice_mpf_incoerente_nao_completa_o_arquivo`, `test_indice_mpf_valido_cobre_as_paginas` |
| 15 | HEIC/AVIF (fotos em contêiner ISO-BMFF) desativados junto com os vídeos; M2TS de 192 bytes não encontrado | `test_heic_e_avif_sao_fotos_mesmo_sem_videos`, `test_m2ts_de_192_bytes_e_encontrado`, `test_mts_de_188_bytes_tambem` |
| 16 | WebP sintético declarado `ok` sem decodificar | `test_webp_real_tem_comprimento_exato_e_decodifica`, `test_webp_sintetico_nao_afirma_ok` |
| 17 | Leitura além do fim físico preenchida com zeros (dado inventado) | `test_leitura_alem_do_fim_nao_inventa_zeros` |
| 18 | Bloco de varredura maior que o limite de memória de 8 MiB | `test_nenhuma_leitura_passa_de_8mib`, `test_leituras_bloqueadas_limitadas` |
| 19 | Cancelamento na etapa 2 não refletido no resultado | `test_cancelamento_na_etapa2_aparece_no_resultado` |
| 20 | Busca de partições perdidas feita sem avisar o usuário | `test_busca_de_particoes_perdidas_avisada` |
| 21 | **Vazamento de descritor**: `LeitorArquivo` herdava um `fechar()` que só marcava uma flag e nunca fechava o arquivo (reproduzido com `python -X dev`) | `test_leitor_de_arquivo_fecha_o_descritor` |
| 22 | **Rótulo do volume FAT32 gravado com 12 bytes em campo de 11**: o setor de boot saía com 513 bytes, o `FilSysType` era empurrado para o offset 83 e **toda imagem de volume ficava 2 bytes maior** que `tam_setores × tam_setor`. Achado pelo `benchmark.py` ao conferir o tamanho do arquivo de 2 GiB | `test_setor_de_boot_fat32_tem_512_bytes_e_campos_alinhados`, `test_volume_fat32_tem_tamanho_exato`, `test_disco_montado_com_volume_fat32_nao_desloca_as_particoes` |
| 23 | CLI validava `--desde` só no meio da varredura (o usuário descobria o erro de data depois de minutos de trabalho) | `test_cli_data_invalida_explica`, `test_cli_aceita_datas_validas` |
| 24 | `validar_destino(..., criar_se_nao_existe=False)` reclamava de "sem permissão de gravação" quando a pasta simplesmente não existia | `test_validar_destino_inexistente_sem_criar_explica` |
| 25 | A janela dizia "concluída" mesmo quando a recuperação tinha parado após a etapa 1 | `test_pausa_apos_etapa1_habilita_continuar_e_finalizar`, `test_finalizar_na_pausa_encerra_sem_etapa2` |
| 26 | Em `.exe` sem console, `sys.stdout` é `None` e o primeiro `print()` da CLI derrubaria o programa | `test_cli_sem_console_grava_log_em_vez_de_quebrar`, `test_cli_com_console_nao_cria_arquivo_de_log` |
| 27 | Nomes hostis vindos do cartão (`..\..\evil.jpg`) podiam escapar da pasta de destino | `test_nome_invalido_e_saneado` + os testes de `realpath` em `test_fuzz.py` |
| 28 | Destino no mesmo disco físico aceito sem autorização explícita | `test_destino_no_mesmo_disco_da_origem_e_recusado`, `test_cli_bloqueia_imagem_no_mesmo_disco`, `test_nunca_autoriza_mesmo_disco_por_conta_propria` |

---

## 5. Verificações de honestidade (o que o programa afirma)

Estas não são desempenho: são o compromisso de não inventar resultado.

- **Nenhum `ok` sem estrutura completa.** JPEG sem EOI é `parcial` com a
  quantidade de bytes aproveitáveis medida; TIFF com *strips* ausentes não vira
  `ok`; WebP/AVI/PSD/RAF/MKV sintéticos (estruturalmente corretos, mas não
  decodificáveis) saem como `nao_verificado`, nunca `ok`.
- **Fração recuperada é medida**, não estimada: `fracao_recuperada` vem da
  razão entre bytes realmente lidos e o tamanho declarado, e o motor ainda
  limita a leitura ao tamanho físico do leitor (`_ler_candidato`).
- **Setor ilegível não vira zero.** Na aquisição, o bloco é preenchido com
  `0xF8` e registrado no mapa como erro — o relatório final diz quantos.
- **`res.recuperados == len(res.candidatos)`** em todas as execuções de teste:
  o número anunciado é o número de arquivos gravados.
- **Cada estado tem um `motivo` em português** no CSV, incluindo os casos
  heurísticos (ex.: *"clusters da cadeia realocados (ex.: 3)"*).

---

## 6. O que NÃO foi validado (limites honestos)

1. **Nenhum teste rodou no Windows.** O ambiente é Linux. Isso afeta:
   - a abertura de `\\.\PhysicalDriveN` e `CloseHandle` (`LeitorDispositivo`),
     que só existe no caminho Windows do código;
   - `listar_dispositivos` com letras de unidade (o teste correspondente é o
     único pulo da suíte);
   - `_disco_fisico_do_caminho_destino`, que no Windows usa o número do disco
     da unidade;
   - os três `.bat` e o `RecuperaFotos.spec`, que foram escritos seguindo a
     documentação do PyInstaller mas **não foram executados aqui**.
2. **Nenhuma mídia física foi usada.** Tudo foi medido sobre arquivos de imagem
   construídos pelo próprio projeto (`tests/construtor.py`). Cartões reais
   trazem situações que o construtor não imita: desgaste de NAND, controlador
   que reordena blocos, `exFAT` com *bitmap* de alocação parcial, NTFS
   comprimido/criptografado, MFT fragmentada em disco real.
3. **Os sistemas de arquivos são sintéticos.** São estruturalmente corretos
   (por isso os testes de NTFS/exFAT/FAT conferem campos byte a byte contra
   MS-FNTFS/MS-FAT32), mas um cartão formatado por uma câmera real tem
   detalhes que só a mídia mostra.
4. **A leitura bruta de 5.198 MB/s é cache de página**, como está dito na
   seção 3. A vazão real dos cenários (88–124 MB/s) é limitada por CPU/Python,
   e não representa um cartão SD de verdade.
5. **HEIC/AVIF/CR3 são validados no nível de contêiner ISO-BMFF**, não do
   fluxo HEVC: o programa acha o arquivo e mede seu comprimento, mas não
   decodifica a imagem (o Pillow comum também não decodifica HEIC sem o plugin).
6. **Vídeos fragmentados** (MP4 com `moov` no fim, gravado em pedaços) não são
   remontados — declarado no LEIA-ME.
7. **Concorrência com antivírus/backup** no Windows (que podem segurar o
   arquivo de destino) não foi testada.
8. **Interface gráfica com Tk real**: os 61 testes rodam contra o duplo
   `tests/tk_stub.py`. O comportamento de layout, foco e redimensionamento do
   Tk verdadeiro não é coberto — só a lógica da janela, os callbacks, a fila de
   threads e o fluxo de recuperação.

---

## 7. Conclusão

- 428 testes automatizados: **427 aprovados, 0 reprovados, 1 pulo justificado**
  (só Windows).
- Em uma imagem real de 2 GiB com gabarito SHA-256: **100% das fotos apagadas e
  100% das fotos órfãs recuperadas byte a byte idênticas**, **0 falso
  positivo**, 0 duplicata, 0 erro de leitura, origem intacta, 26 MB de memória
  e 2 GiB varridos em 23 s neste ambiente.
- 28 defeitos reais encontrados e corrigidos, cada um com teste de regressão.
- As limitações da seção 6 são de **ambiente** (sem Windows, sem mídia física),
  não de cobertura de código: onde foi possível executar, foi executado.
