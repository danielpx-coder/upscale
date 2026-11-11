#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Recupera Fotos 4.0 — ponto de entrada (CLI + interface gráfica).

Sem parâmetros, abre a janela gráfica. Exemplos:

  python recupera_fotos.py --listar --todos-os-discos
  python recupera_fotos.py F: -o E:\\Recuperadas
  python recupera_fotos.py disco2 -o E:\\Recuperadas --videos
  python recupera_fotos.py disco2 -o E:\\Recuperadas --buscar-particoes
  python recupera_fotos.py disco2 -o E:\\Recuperadas --criar-imagem E:\\origem.img
  python recupera_fotos.py disco2 -o E:\\Recuperadas --criar-imagem E:\\origem.img --retomar-imagem
  python recupera_fotos.py origem.img -o E:\\Recuperadas --pasta-relativa Fotos
  python recupera_fotos.py origem.img -o E:\\Recuperadas --profunda --min-kb 0

  --desde, --ignorar-sem-data, --sem-pastas, --sem-verificar,
  --sem-data-no-nome, --cartao-inteiro (alias --unidade-inteira)
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time

# permite execução direta do arquivo sem instalação
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aquisicao
import motor


def _formatar_bytes(n: float) -> str:
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} PB"


def montar_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="recupera_fotos.py",
        description="Recupera Fotos 4.0 — recuperação de fotos e vídeos "
                    "apagados (FAT16/FAT32/exFAT/NTFS + varredura por "
                    "assinatura), somente-leitura na origem.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Exemplos:")[-1])
    p.add_argument("origem", nargs="?", default=None,
                   help="letra (E:), disco (disco2), caminho \\\\.\\PhysicalDriveN, "
                        "/dev/sdX, arquivo de imagem ou pasta")
    p.add_argument("-o", "--destino", default=None,
                   help="pasta onde salvar os arquivos recuperados (outro disco)")
    p.add_argument("--listar", action="store_true",
                   help="lista os dispositivos detectados e sai")
    p.add_argument("--todos-os-discos", action="store_true",
                   help="com --listar, mostra também discos sem unidades")
    p.add_argument("--videos", action="store_true",
                   help="recuperar também vídeos")
    p.add_argument("--buscar-particoes", action="store_true",
                   help="procurar partições perdidas")
    p.add_argument("--criar-imagem", metavar="CAMINHO", default=None,
                   help="cria uma imagem forense da origem (outro disco) e sai")
    p.add_argument("--retomar-imagem", action="store_true",
                   help="retoma uma aquisição interrompida (verifica blocos)")
    p.add_argument("--pasta-relativa", metavar="PASTA", default=None,
                   help="filtra arquivos cuja pasta original (nos metadados) "
                        "corresponda a este caminho")
    p.add_argument("--profunda", action="store_true",
                   help="busca profunda (varredura de metadados órfãos)")
    p.add_argument("--min-kb", type=int, default=None,
                   help="tamanho mínimo em KB (0 = sem mínimo; padrão: 8)")
    p.add_argument("--desde", default=None,
                   help="filtro de data: hoje | ontem | Nd (ex.: 7d) | DD/MM/AAAA")
    p.add_argument("--ignorar-sem-data", action="store_true",
                   help="descarta arquivos sem data conhecida")
    p.add_argument("--sem-pastas", action="store_true",
                   help="não recria a estrutura de pastas original")
    p.add_argument("--sem-verificar", action="store_true",
                   help="não classifica a integridade (tudo vira nao_verificado)")
    p.add_argument("--sem-data-no-nome", action="store_true",
                   help="não prefixa data no nome dos arquivos de varredura")
    p.add_argument("--cartao-inteiro", action="store_true",
                   help="varredura por assinatura em todo o volume")
    p.add_argument("--unidade-inteira", action="store_true",
                   help="alias de --cartao-inteiro")
    p.add_argument("--espaco-livre", action="store_true", default=None,
                   help="varredura apenas no espaço livre (padrão)")
    p.add_argument("--volume-todo", action="store_true",
                   help="varredura em todo o volume (não apenas espaço livre)")
    p.add_argument("--pausar", action="store_true",
                   help="(CLI) imprime aviso e encerra após a etapa 1")
    p.add_argument("--permitir-mesmo-disco", action="store_true",
                   help="autoriza destino no mesmo disco APENAS quando a origem "
                        "é um arquivo de imagem (cópia), não a mídia original")
    p.add_argument("--limite", type=int, default=None,
                   help="número máximo de arquivos a recuperar")
    return p


def cmd_listar(todos: bool) -> int:
    dispositivos = aquisicao.listar_dispositivos(todos=todos)
    if not dispositivos:
        print("Nenhum dispositivo detectado.")
        return 0
    print(f"{'id':<10} {'nome':<24} {'capacidade':>12}  unidades  flags")
    for d in dispositivos:
        flags = []
        if d.get("removivel"):
            flags.append("removível")
        if d.get("disco_sistema"):
            flags.append("DISCO DO SISTEMA")
        cap = _formatar_bytes(d["capacidade"]) if d.get("capacidade") else "?"
        print(f"{d['id']:<10} {(d.get('modelo') or d['nome'])[:24]:<24} "
              f"{cap:>12}  {','.join(d.get('unidades') or []) or '-':<8}  "
              f"{'; '.join(flags)}")
    return 0


def cmd_criar_imagem(origem_entrada: str, caminho_img: str, retomar: bool,
                     permitir_mesmo_disco: bool = False) -> int:
    leitor = aquisicao.abrir_origem(origem_entrada)
    if isinstance(leitor, aquisicao.OrigemPasta):
        if leitor.leitor is None:
            print("ERRO: não foi possível abrir o volume físico da pasta.")
            return 2
        leitor = leitor.leitor
    # segurança: imagem não pode ficar no mesmo disco da origem
    pasta_img = os.path.dirname(os.path.abspath(caminho_img)) or "."
    os.makedirs(pasta_img, exist_ok=True)
    mesmo, motivo = aquisicao.destino_mesmo_disco_fisico(leitor, pasta_img)
    if mesmo is not False:
        if permitir_mesmo_disco:
            print(f"AVISO: {motivo}; gravação autorizada explicitamente pelo "
                  f"usuário (--permitir-mesmo-disco). A origem é aberta somente "
                  f"para leitura.")
        else:
            leitor.fechar()
            print(f"ERRO: a imagem seria gravada no mesmo disco da origem "
                  f"({motivo}). Escolha outro disco físico.")
            return 2
    ultima = [0.0]

    def progresso(bytes_lidos, total, fase):
        agora = time.time()
        if agora - ultima[0] >= 1.0 or bytes_lidos >= total:
            ultima[0] = agora
            pct = 100.0 * bytes_lidos / max(total, 1)
            print(f"\r  aquisição: {pct:5.1f}%  "
                  f"({_formatar_bytes(bytes_lidos)} / {_formatar_bytes(total)})   ",
                  end="", flush=True)
    adq = aquisicao.Adquiridor(leitor, caminho_img, progresso=progresso)
    try:
        resumo = adq.executar(retomar=retomar)
    except FileExistsError as exc:
        print(f"\nERRO: {exc}")
        leitor.fechar()
        return 2
    leitor.fechar()
    print()
    print(f"Imagem criada: {caminho_img}")
    print(f"  blocos ok: {resumo['ok']}  zerados: {resumo['zero']}  "
          f"com erro de leitura: {resumo['erro']}")
    if resumo.get("revalidados"):
        print(f"  blocos revalidados na retomada: {resumo['revalidados']}")
    print(f"  mapa: {caminho_img}.map.jsonl (versão {aquisicao.MAPA_VERSAO})")
    if resumo["erro"]:
        print("  ATENÇÃO: setores ilegíveis foram registrados no mapa e "
              "preenchidos com 0xF8; não são setores zerados.")
    return 0


def cmd_recuperar(args) -> int:
    if not args.destino:
        print("ERRO: informe o destino com -o/--destino "
              "(pasta em OUTRO disco físico).")
        return 2
    try:
        motor.parsear_desde(args.desde or "")
    except ValueError as exc:
        print(f"ERRO: {exc}")
        return 2
    op = motor.Opcoes(
        origem=args.origem,
        destino=args.destino,
        videos=args.videos,
        apenas_espaco_livre=not (args.volume_todo or args.cartao_inteiro
                                 or args.unidade_inteira),
        verificar=not args.sem_verificar,
        profunda=args.profunda,
        incluir_pequenos=(args.min_kb == 0) if args.min_kb is not None else False,
        min_kb=args.min_kb,
        pausar_apos_etapa1=False,
        buscar_particoes=args.buscar_particoes,
        desde=args.desde or "",
        ignorar_sem_data=args.ignorar_sem_data,
        sem_pastas=args.sem_pastas,
        sem_verificar=args.sem_verificar,
        sem_data_no_nome=args.sem_data_no_nome,
        cartao_inteiro=args.cartao_inteiro or args.unidade_inteira,
        pasta_relativa=args.pasta_relativa or "",
        permitir_mesmo_disco=args.permitir_mesmo_disco,
        limite_arquivos=args.limite,
        etapa2=not args.pausar,
    )
    ultima = [0.0]

    def progresso(fracao, total, fase):
        agora = time.time()
        if agora - ultima[0] >= 0.5 or fracao >= 1.0:
            ultima[0] = agora
            print(f"\r  [{fase}] {fracao * 100:5.1f}%   ", end="", flush=True)

    cancelar = threading.Event()
    try:
        m = motor.Motor(op, progresso=progresso, log=print, cancelar=cancelar)
        res = m.executar()
    except PermissionError as exc:
        print(f"\nERRO: {exc}")
        return 2
    except ValueError as exc:
        print(f"\nERRO de parâmetro: {exc}")
        return 2
    print()
    print("=" * 64)
    print("RESULTADO DA RECUPERAÇÃO")
    print("=" * 64)
    print(f"  Sistemas de arquivos: {', '.join(res.sistemas) or 'nenhum'}")
    print(f"  Arquivos recuperados: {res.recuperados} "
          f"({_formatar_bytes(res.bytes_recuperados)})")
    print(f"  Por estado: ok={res.por_estado.get('ok', 0)}  "
          f"nao_verificado={res.por_estado.get('nao_verificado', 0)}  "
          f"corrompido={res.por_estado.get('corrompido', 0)}  "
          f"parcial={res.por_estado.get('parcial', 0)}")
    print(f"  Por método: {res.por_metodo or '{}'}")
    print(f"  Duplicatas evitadas: hash={res.duplicados_hash} "
          f"região={res.duplicados_regiao}")
    print(f"  Erros de leitura: {res.erros_leitura}")
    print(f"  Candidatos rejeitados pelos filtros: {res.rejeitados}")
    print(f"  Tempo: {res.tempo_segundos:.1f}s")
    for a in res.avisos:
        print(f"  AVISO: {a}")
    if res.relatorio_csv:
        print(f"  Relatório: {res.relatorio_csv}")
    if res.diagnostico_csv:
        print(f"  Diagnóstico NTFS: {res.diagnostico_csv}")
    return 0


def _garantir_saida_padrao() -> str | None:
    """Devolve o caminho do arquivo de log, ou None se a saída já existe.

    Um .exe gerado sem console (o modo janela do PyInstaller) tem
    sys.stdout/sys.stderr iguais a None — e qualquer print() derruba o
    programa com AttributeError. Como a CLI precisa continuar funcionando no
    mesmo executável, a saída é desviada para recupera_fotos_log.txt, ao lado
    do programa, e o usuário é avisado do caminho no fim.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return None
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable or "."))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    caminho = os.path.join(base, "recupera_fotos_log.txt")
    try:
        arq = open(caminho, "a", encoding="utf-8", buffering=1)
    except OSError:
        try:
            arq = open(os.devnull, "w", encoding="utf-8")
        except OSError:
            return None
        caminho = None
    if sys.stdout is None:
        sys.stdout = arq
    if sys.stderr is None:
        sys.stderr = arq
    return caminho


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    log_exe = _garantir_saida_padrao()
    codigo = _despachar(argv)
    if log_exe:
        print(f"\n(saída desta execução gravada em {log_exe})")
    return codigo


def _despachar(argv: list[str]) -> int:
    parser = montar_parser()
    args = parser.parse_args(argv)

    if args.min_kb is not None and args.min_kb < 0:
        print("ERRO: --min-kb não pode ser negativo.")
        return 2
    if args.desde:
        # a mesma função usada pelo motor: uma única fonte de verdade
        try:
            motor.parsear_desde(args.desde)
        except ValueError:
            print(f"ERRO: --desde inválido ({args.desde!r}). Use hoje, ontem, "
                  "Nd (ex.: 7d) ou DD/MM/AAAA.")
            return 2

    if args.listar:
        return cmd_listar(args.todos_os_discos)

    if not args.origem:
        # sem parâmetros: abre a interface gráfica
        try:
            import interface
        except Exception as exc:
            print(f"ERRO ao carregar a interface gráfica: {exc}")
            print("Use a CLI: python recupera_fotos.py --help")
            return 1
        interface.executar_gui()
        return 0

    if args.criar_imagem:
        return cmd_criar_imagem(args.origem, args.criar_imagem,
                                args.retomar_imagem, args.permitir_mesmo_disco)

    if args.retomar_imagem:
        print("ERRO: --retomar-imagem só é válido junto com --criar-imagem.")
        return 2

    return cmd_recuperar(args)


if __name__ == "__main__":
    sys.exit(main())
