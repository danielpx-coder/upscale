"""
rodar_testes.py — executa TODA a suíte de testes e grava o relatório real.

Uso:
    python rodar_testes.py                 # roda tudo e grava resultado_testes.txt
    python rodar_testes.py test_ntfs       # roda só um módulo
    python rodar_testes.py --sem-gravar    # só mostra na tela

O arquivo resultado_testes.txt é a saída VERDADEIRA do comando (nada é escrito
à mão): data, plataforma, versões, a lista de testes com o resultado de cada
um, os pulos com o motivo e o resumo final.
"""

from __future__ import annotations

import io
import os
import platform
import sys
import time
import unittest

RAIZ = os.path.dirname(os.path.abspath(__file__))
TESTES = os.path.join(RAIZ, "tests")
RELATORIO = os.path.join(RAIZ, "resultado_testes.txt")

MODULOS = ["test_aquisicao", "test_ntfs", "test_advanced_ntfs",
           "test_regressions", "test_fuzz", "test_recupera_fotos",
           "test_interface"]


def _versao_pillow() -> str:
    try:
        import PIL
        return PIL.__version__
    except ImportError:
        return "ausente (os verificadores estruturais próprios continuam ativos)"


def _cabecalho(argv: list[str]) -> str:
    return "\n".join([
        "=" * 72,
        "RECUPERA FOTOS 4.0 — RESULTADO REAL DA SUÍTE DE TESTES",
        "=" * 72,
        f"comando          : python rodar_testes.py {' '.join(argv[1:])}".rstrip(),
        f"data/hora        : {time.strftime('%d/%m/%Y %H:%M:%S')}",
        f"sistema          : {platform.system()} {platform.release()} "
        f"({platform.machine()})",
        f"python           : {sys.version.split()[0]} "
        f"({platform.python_implementation()})",
        f"Pillow           : {_versao_pillow()}",
        f"cpus / memória   : {os.cpu_count()} núcleo(s)",
        f"pasta dos testes : {TESTES}",
        "=" * 72,
        "",
    ])


def _carregar(modulos: list[str] | None) -> list[tuple[str, unittest.TestSuite]]:
    """Uma suíte por módulo — assim o resumo é contado por objeto, não por texto."""
    sys.path.insert(0, RAIZ)
    sys.path.insert(0, TESTES)
    loader = unittest.TestLoader()
    nomes = modulos or sorted(
        n[:-3] for n in os.listdir(TESTES)
        if n.startswith("test_") and n.endswith(".py"))
    pares = []
    for nome in nomes:
        suíte = loader.loadTestsFromName(nome)
        # loadTestsFromName pode devolver um _FailedTest: mantém, para o erro aparecer
        pares.append((nome, suíte))
    return pares


def rodar(modulos: list[str] | None = None):
    """Executa módulo a módulo. Devolve (linhas_por_módulo, tabela, totais, tempo)."""
    pares = _carregar(modulos)
    buffer_saida = io.StringIO()
    runner = unittest.TextTestRunner(stream=buffer_saida, verbosity=2)
    tabela = []          # (módulo, executados, ok, falhas, erros, pulos)
    todos_pulados = []
    inicio = time.time()
    for nome, suíte in pares:
        resultado = runner.run(suíte)
        falhas = len(resultado.failures)
        erros = len(resultado.errors)
        pulos = len(resultado.skipped)
        ok = resultado.testsRun - falhas - erros - pulos
        tabela.append((nome, resultado.testsRun, ok, falhas, erros, pulos))
        todos_pulados.extend((t.id().split(".")[-1], motivo)
                             for t, motivo in resultado.skipped)
        buffer_saida.write("\n")
    duracao = time.time() - inicio
    totais = {
        "executados": sum(linha[1] for linha in tabela),
        "ok": sum(linha[2] for linha in tabela),
        "falhas": sum(linha[3] for linha in tabela),
        "erros": sum(linha[4] for linha in tabela),
        "pulos": sum(linha[5] for linha in tabela),
    }
    return buffer_saida.getvalue(), tabela, totais, todos_pulados, duracao


def resumo_por_modulo(tabela: list, totais: dict) -> str:
    linhas = ["", "-" * 72, "RESUMO POR MÓDULO (contado a partir do resultado real)",
              "-" * 72,
              f"{'módulo':<24}{'exec.':>7}{'ok':>6}{'falhas':>8}{'erros':>7}{'pulos':>7}"]
    for nome, ex, ok, f, e, p in tabela:
        linhas.append(f"{nome:<24}{ex:>7}{ok:>6}{f:>8}{e:>7}{p:>7}")
    linhas.append("-" * 72)
    linhas.append(f"{'TOTAL':<24}{totais['executados']:>7}{totais['ok']:>6}"
                  f"{totais['falhas']:>8}{totais['erros']:>7}{totais['pulos']:>7}")
    return "\n".join(linhas) + "\n"


def main(argv: list[str]) -> int:
    gravar = "--sem-gravar" not in argv
    modulos = [a for a in argv[1:] if not a.startswith("--")] or None
    texto, tabela, totais, pulados, duracao = rodar(modulos)
    falhas = totais["falhas"] + totais["erros"]

    corpo = [_cabecalho(argv), texto, resumo_por_modulo(tabela, totais), "",
             "=" * 72,
             f"RESULTADO FINAL: {totais['executados']} teste(s) executado(s) "
             f"em {duracao:.1f}s",
             f"  aprovados : {totais['ok']}",
             f"  reprovados: {falhas}",
             f"  pulados   : {totais['pulos']}"]
    for nome, motivo in pulados:
        corpo.append(f"     - {nome}: {motivo}")
    corpo.append("  STATUS    : " + ("TODOS OS TESTES PASSARAM" if falhas == 0
                                     else "HÁ FALHAS — veja os blocos FAIL/ERROR acima"))
    corpo.append("=" * 72)
    completo = "\n".join(corpo) + "\n"

    # na tela só o resumo; o detalhe completo fica no arquivo
    print("\n".join(completo.splitlines()[-(20 + len(tabela) + len(pulados)):]))
    if gravar:
        with open(RELATORIO, "w", encoding="utf-8", newline="\n") as f:
            f.write(completo)
        print(f"\nRelatório completo gravado em: {RELATORIO}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
