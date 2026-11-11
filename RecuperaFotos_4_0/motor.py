# -*- coding: utf-8 -*-
"""
motor.py — Motor de recuperação em duas etapas.

ETAPA 1: recuperação por sistema de arquivos (FAT16/FAT32/exFAT/NTFS), com
         nomes, caminhos, datas, tamanhos e fragmentos vindos dos metadados.
ETAPA 2: file carving por assinatura (espaço livre ou volume inteiro), com
         proteção das regiões já recuperadas e deduplicação por hash.

Também implementa: filtros (datas, tamanho mínimo, fotos/vídeos), extração
segura (nomes únicos, gravação restrita ao destino validado), verificação de
integridade com classificação, pasta "_corrompidas", pausa entre etapas,
cancelamento seguro e relatórios CSV (relatorio_recuperacao.csv e
diagnostico_ntfs.csv).

Todas as posições físicas usadas pelo motor são ABSOLUTAS na origem (os
sistemas de arquivos são criados com offset_volume sobre o leitor principal),
o que garante coerência entre intervalos protegidos, espaço livre e carving.
"""

from __future__ import annotations

import csv
import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aquisicao
import assinaturas
import exfat
import fatfs
import ntfs
import particoes
import verificacao
from aquisicao import Intervalos, OrigemPasta, validar_destino
from candidatos import (CandidatoArquivo, ESTADO_CORROMPIDO,
                        ESTADO_NAO_VERIFICADO, ESTADO_OK, ESTADO_PARCIAL,
                        EXTENSOES_FOTO, EXTENSOES_VIDEO, METODO_MFT,
                        METODO_MFT_ORFA, METODO_PASTA, METODO_VARREDURA,
                        Fragmento, nome_unico)

PASTA_CORROMPIDAS = "_corrompidas"
COLUNAS_CSV = ["arquivo", "tipo", "encontrado_por", "caminho_original",
               "posicao_no_cartao", "tamanho_bytes", "data", "estado",
               "fracao_recuperada", "metodo", "motivo"]
COLUNAS_DIAG = ["registro_MFT", "nome", "motivo"]


def parsear_desde(desde: str) -> "datetime | None":
    """Converte a opção de período em um limite de data (None = sem filtro).

    Aceita: "hoje", "ontem"/"ontem_e_hoje", "Nd" (últimos N dias) e
    DD/MM/AAAA ou DD/MM/AA. Qualquer outra coisa levanta ValueError com
    mensagem entendível — a CLI e a interface validam ANTES de começar, para
    o usuário não descobrir o erro no fim de uma varredura longa.
    """
    texto = (desde or "").strip().lower()
    if not texto:
        return None
    hoje = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if texto == "hoje":
        return hoje
    if texto in ("ontem", "ontem_e_hoje"):
        return hoje - timedelta(days=1)
    if texto.endswith("d") and texto[:-1].isdigit():
        return hoje - timedelta(days=int(texto[:-1]))
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"formato de data inválido: {desde!r} "
        "(use hoje, ontem, Nd ou DD/MM/AAAA)")


@dataclass
class Opcoes:
    origem: str = ""
    destino: str = ""
    videos: bool = False
    apenas_espaco_livre: bool = True
    verificar: bool = True
    profunda: bool = False
    incluir_pequenos: bool = False     # arquivos < 8 KB
    pausar_apos_etapa1: bool = False
    buscar_particoes: bool = False
    desde: str = ""                    # '' | 'hoje' | 'ontem' | '7d' | '30d' | DD/MM/AAAA
    ignorar_sem_data: bool = False     # excluir arquivos sem data conhecida
    sem_pastas: bool = False           # não recriar estrutura de pastas
    sem_verificar: bool = False
    sem_data_no_nome: bool = False
    cartao_inteiro: bool = False       # carving em todo o volume
    pasta_relativa: str = ""           # filtro de pasta
    min_kb: int | None = None          # None = padrão (8 KB se não incluir_pequenos)
    permitir_mesmo_disco: bool = False
    limite_arquivos: int | None = None
    etapa2: bool = True


@dataclass
class ResultadoRecuperacao:
    recuperados: int = 0
    bytes_recuperados: int = 0
    erros_leitura: int = 0
    duplicados_hash: int = 0
    duplicados_regiao: int = 0
    rejeitados: int = 0
    por_estado: dict = field(default_factory=lambda: {
        ESTADO_OK: 0, ESTADO_NAO_VERIFICADO: 0, ESTADO_CORROMPIDO: 0,
        ESTADO_PARCIAL: 0})
    por_metodo: dict = field(default_factory=dict)
    sistemas: list = field(default_factory=list)
    volumes: list = field(default_factory=list)
    candidatos: list = field(default_factory=list)   # limitados em memória
    recusados: list = field(default_factory=list)    # (candidato, motivo)
    recusas_ntfs: list = field(default_factory=list)
    avisos: list = field(default_factory=list)
    tempo_segundos: float = 0.0
    cancelado: bool = False
    pausado: bool = False
    relatorio_csv: str = ""
    diagnostico_csv: str = ""


class Motor:
    MAX_CANDIDATOS_MEMORIA = 5000

    def __init__(self, opcoes: Opcoes, progresso=None, log=None,
                 cancelar: threading.Event | None = None,
                 continuar: threading.Event | None = None,
                 finalizar: threading.Event | None = None):
        self.op = opcoes
        self.progresso = progresso or (lambda fracao, total, fase: None)
        self.log = log or (lambda msg: None)
        self.cancelar = cancelar or threading.Event()
        self.continuar = continuar or threading.Event()
        self.finalizar = finalizar or threading.Event()
        self.resultado = ResultadoRecuperacao()
        self.protegidos = Intervalos()
        self._hashes: set[str] = set()
        self._nomes_usados: set[str] = set()
        self._inicio = 0.0
        self.leitor_principal = None
        self._volumes: list[dict] = []
        self._limite_data = "NAO_CALCULADO"
        self._fs_atual = None

    # ------------------------------------------------------------------ util

    def _fase(self, fracao: float, fase: str):
        self.progresso(min(1.0, max(0.0, fracao)), 1.0, fase)

    def _registrado(self, msg: str):
        self.log(msg)

    # --------------------------------------------------------------- execução

    def executar(self) -> ResultadoRecuperacao:
        self._inicio = time.time()
        op = self.op
        res = self.resultado

        origem_resolvida = aquisicao.abrir_origem(op.origem) if op.origem else None
        valid = validar_destino(origem_resolvida, op.destino,
                                permitir_mesmo_disco=op.permitir_mesmo_disco)
        if not valid.ok:
            res.avisos.append(f"destino recusado: {valid.motivo}")
            self._registrado(f"ERRO: destino recusado: {valid.motivo}")
            self._fechar(origem_resolvida)
            res.tempo_segundos = time.time() - self._inicio
            raise PermissionError(valid.motivo)
        for a in valid.advertencias:
            res.avisos.append(a)
        os.makedirs(op.destino, exist_ok=True)

        try:
            self._etapa1(origem_resolvida)
            if self.cancelar.is_set():
                res.cancelado = True
            if op.pausar_apos_etapa1 and not self.cancelar.is_set():
                res.pausado = True
                self._registrado("Pausado após a etapa 1. Aguardando decisão "
                                 "(Continuar etapa 2 / Finalizar).")
                while not (self.continuar.is_set() or self.finalizar.is_set()
                           or self.cancelar.is_set()):
                    time.sleep(0.1)
                if self.finalizar.is_set() or self.cancelar.is_set():
                    res.cancelado = self.cancelar.is_set()
                    self._fechar(origem_resolvida)
                    res.tempo_segundos = time.time() - self._inicio
                    self._gerar_relatorios()
                    return res
            if op.etapa2 and not self.cancelar.is_set():
                self._etapa2()
            if self.cancelar.is_set():
                res.cancelado = True
        finally:
            self._fechar(origem_resolvida)
        res.tempo_segundos = time.time() - self._inicio
        self._gerar_relatorios()
        return res

    def _fechar(self, origem_resolvida=None):
        """Fecha TODOS os leitores abertos nesta execução. Um descritor
        esquecido trava o cartão no Windows (não deixa ejetar) e vaza arquivos
        quando o usuário roda várias recuperações seguidas."""
        alvos = []
        if isinstance(origem_resolvida, OrigemPasta):
            if origem_resolvida.leitor is not None:
                alvos.append(origem_resolvida.leitor)
        elif origem_resolvida is not None:
            alvos.append(origem_resolvida)
        if self.leitor_principal is not None:
            alvos.append(self.leitor_principal)
        for leitor in alvos:
            try:
                leitor.fechar()
            except Exception:
                pass
        self.leitor_principal = None

    # ------------------------------------------------------------- ETAPA 1

    def _etapa1(self, origem_resolvida):
        op = self.op
        res = self.resultado
        self._registrado(f"Etapa 1: recuperação por sistema de arquivos "
                         f"(origem: {op.origem})")
        volumes = self._resolver_volumes(origem_resolvida)
        self._volumes = volumes
        if not volumes:
            res.avisos.append("nenhum volume reconhecido; a etapa 2 usará apenas "
                              "varredura por assinatura")
            self._registrado("AVISO: nenhum sistema de arquivos reconhecido.")
            return
        total = 0
        for volume in volumes:
            if self.cancelar.is_set():
                break
            total += self._processar_volume(volume)
        self._registrado(f"Etapa 1 concluída: {total} candidato(s) processado(s).")

    def _resolver_volumes(self, origem_resolvida) -> list[dict]:
        op = self.op
        res = self.resultado
        volumes: list[dict] = []

        if isinstance(origem_resolvida, OrigemPasta):
            if origem_resolvida.leitor is None:
                res.avisos.append(
                    "não foi possível abrir o volume físico da pasta escolhida; "
                    "a recuperação por metadados ficou indisponível")
                self._registrado("AVISO: volume físico da pasta inacessível "
                                 "(no Windows, execute como administrador).")
                return volumes
            leitor = origem_resolvida.leitor
            self.leitor_principal = leitor
            off = origem_resolvida.offset_volume
            vol = self._montar_volume(leitor, off, leitor.tamanho - off, "pasta")
            if vol:
                vol["filtro_pasta"] = origem_resolvida.pasta
                volumes.append(vol)
            else:
                volumes.extend(self._particoes_do_disco(leitor))
            return volumes

        leitor = origem_resolvida
        self.leitor_principal = leitor
        # volume direto no início do disco/imagem (cartões sem tabela)
        vol = self._montar_volume(leitor, 0, leitor.tamanho, "volume")
        if vol:
            volumes.append(vol)
            res.volumes.append(f"{vol['tipo']}@0")
            return volumes

        parts = particoes.detectar_particoes(
            leitor, setor=leitor.setor_size,
            buscar_perdidas=op.buscar_particoes,
            cancelar=self.cancelar,
            progresso=lambda f, t, fase: self._fase(f * 0.05, fase))
        for p in parts:
            if self.cancelar.is_set():
                break
            base = p.inicio
            boot, t, info, origem_boot = particoes.ler_boot_com_backup(leitor, p)
            if t is None:
                # tipo declarado pela tabela, mas boot ilegível: tenta direto
                vol = self._montar_volume(leitor, base, p.tamanho, p.origem)
            else:
                if origem_boot != "primario":
                    res.avisos.append(
                        f"boot primário inválido em {base}; cópia de reserva usada "
                        f"({origem_boot})")
                    self._registrado(
                        f"AVISO: boot de reserva usado ({origem_boot}) para "
                        f"{t}@{base}")
                vol = self._criar_fs(leitor, t, info, base, p.tamanho)
            if vol:
                volumes.append(vol)
                res.volumes.append(f"{vol['tipo']}@{base}")
        if not volumes:
            res.avisos.append("nenhuma partição válida na tabela; busca automática "
                              "por partições perdidas acionada")
            self._registrado("AVISO: nenhuma partição válida; acionando busca "
                             "automática por partições perdidas.")
            perdidas = particoes.buscar_particoes_perdidas(
                leitor, setor=leitor.setor_size, cancelar=self.cancelar)
            for p in perdidas:
                vol = self._montar_volume(leitor, p.inicio, p.tamanho, "perdida")
                if vol:
                    volumes.append(vol)
                    res.volumes.append(f"{vol['tipo']}@{p.inicio}(perdida)")
            if volumes:
                res.avisos.append(
                    f"{len(volumes)} partição(ões) perdida(s) localizada(s) pela "
                    "busca de setores de boot")
        elif any(getattr(p, "origem", "") == "perdida" for p in parts):
            achadas = [p for p in parts if getattr(p, "origem", "") == "perdida"]
            res.avisos.append(
                f"{len(achadas)} partição(ões) localizada(s) pela busca de "
                "partições perdidas (tabela de partições ausente ou inválida)")
        return volumes

    def _montar_volume(self, leitor, base: int, tamanho: int, origem: str):
        for setor_tentativa in (512, 4096):
            boot = leitor.read(base, setor_tentativa)
            t, info = particoes.identificar_boot(boot)
            if t:
                return self._criar_fs(leitor, t, info, base, tamanho)
        # boot primário ilegível: tenta as cópias de reserva (FAT32 setor 6,
        # exFAT setores 12..23 com checksum, NTFS último setor do volume)
        pseudo = particoes.Particao(base, tamanho, "", origem)
        setor_bytes, t, info, origem_boot = particoes.ler_boot_com_backup(leitor, pseudo)
        if t:
            self.resultado.avisos.append(
                f"boot primário inválido em {base}; cópia de reserva usada "
                f"({origem_boot})")
            self._registrado(f"AVISO: boot de reserva usado ({origem_boot}) "
                             f"para {t}@{base}")
            return self._criar_fs(leitor, t, info, base, tamanho)
        return None

    def _criar_fs(self, leitor, tipo: str, info: dict, base: int, tamanho: int):
        res = self.resultado
        if tipo in ("fat16", "fat32"):
            fs = fatfs.SistemaFAT(leitor, info, base, tamanho)
        elif tipo == "exfat":
            fs = exfat.SistemaExFAT(leitor, info, base, tamanho)
        elif tipo == "ntfs":
            fs = ntfs.SistemaNTFS(leitor, info, base, tamanho)
        else:
            return None
        if tipo not in res.sistemas:
            res.sistemas.append(tipo)
        return {"leitor": leitor, "tipo": tipo, "info": info, "fs": fs,
                "base": base, "tamanho": tamanho}

    def _particoes_do_disco(self, leitor) -> list[dict]:
        parts = particoes.detectar_particoes(leitor, setor=leitor.setor_size,
                                             buscar_perdidas=True,
                                             cancelar=self.cancelar)
        volumes = []
        for p in parts:
            vol = self._montar_volume(leitor, p.inicio, p.tamanho, p.origem)
            if vol:
                volumes.append(vol)
                self.resultado.volumes.append(f"{vol['tipo']}@{p.inicio}")
        return volumes

    # --------------------------------------------------- processar um volume

    def _processar_volume(self, volume: dict) -> int:
        op = self.op
        res = self.resultado
        fs = volume["fs"]
        tipo = volume["tipo"]
        leitor = volume["leitor"]
        self._fs_atual = fs
        self._registrado(f"Sistema de arquivos identificado: {tipo}")
        filtro_pasta = volume.get("filtro_pasta")
        filtro_rel = op.pasta_relativa or ""
        filtrar = bool(filtro_pasta or filtro_rel)

        if tipo == "ntfs":
            cands, recusas = fs.recuperar(
                cancelar=self.cancelar, incluir_vivos=False, orfaos=True,
                progresso=lambda f, t, fase: self._fase(0.05 + f * 0.45, fase),
                filtro_caminho=None)
            res.recusas_ntfs.extend(recusas)
            res.erros_leitura += leitor.errores_leitura
        else:
            cands = fs.recuperar(
                cancelar=self.cancelar, incluir_vivos=False,
                profunda=op.profunda,
                progresso=lambda f, t, fase: self._fase(0.05 + f * 0.45, fase))
            res.erros_leitura += leitor.errores_leitura

        if filtrar:
            antes = len(cands)
            cands = self._filtrar_por_pasta(cands, filtro_pasta, filtro_rel)
            self._registrado(f"Filtro de pasta: {antes} -> {len(cands)} candidato(s).")

        processados = 0
        for cand in cands:
            if self.cancelar.is_set():
                break
            if op.limite_arquivos and res.recuperados >= op.limite_arquivos:
                break
            if not self._candidato_passa_filtros(cand, leitor):
                continue
            processados += 1
            self._extrair_e_verificar(cand, leitor)
        self._fs_atual = None
        return processados

    def _filtrar_por_pasta(self, cands, filtro_pasta, filtro_rel):
        """Filtra por pasta usando NOMES E RELAÇÕES DE DIRETÓRIO dos metadados
        do sistema de arquivos — nunca os.walk(). Quando o caminho original não
        pôde ser determinado, o arquivo NÃO é atribuído à pasta escolhida."""
        alvo = None
        if filtro_pasta:
            _drive, caminho = os.path.splitdrive(os.path.normpath(filtro_pasta))
            alvo = caminho.replace("\\", "/").strip("/").lower()
        elif filtro_rel:
            alvo = filtro_rel.replace("\\", "/").strip("/").lower()
        if alvo is None:
            return cands
        saida = []
        for c in cands:
            cam = (c.caminho_original or "").replace("\\", "/").strip("/").lower()
            if not cam:
                continue  # caminho indeterminado: não atribuir arbitrariamente
            if cam == alvo or cam.startswith(alvo + "/") or \
                    ("/" + alvo + "/") in ("/" + cam + "/") or cam.endswith("/" + alvo):
                saida.append(c)
        return saida

    # ---------------------------------------------------------------- filtros

    def _data_limite(self):
        if self._limite_data != "NAO_CALCULADO":
            return self._limite_data
        self._limite_data = parsear_desde(self.op.desde)
        return self._limite_data

    def _min_bytes(self) -> int:
        op = self.op
        if op.min_kb == 0:
            return 0
        if op.min_kb is not None:
            return op.min_kb * 1024
        return 0 if op.incluir_pequenos else 8 * 1024

    def _candidato_passa_filtros(self, cand: CandidatoArquivo, leitor=None) -> bool:
        op = self.op
        res = self.resultado
        ext = cand.extensao
        if ext not in EXTENSOES_FOTO:
            if not (op.videos and ext in EXTENSOES_VIDEO):
                res.rejeitados += 1
                return False
        if cand.tamanho and cand.tamanho < self._min_bytes():
            res.rejeitados += 1
            return False
        try:
            limite = self._data_limite()
        except ValueError as exc:
            res.avisos.append(str(exc))
            self._limite_data = None
            limite = None
        if limite is not None:
            dt = cand.data
            if dt is None and leitor is not None and ext in EXTENSOES_FOTO:
                dt = self._data_exif_da_origem(cand, leitor)
            if dt is None:
                if op.ignorar_sem_data:
                    res.rejeitados += 1
                    return False
            elif dt < limite:
                res.rejeitados += 1
                return False
        return True

    def _data_exif_da_origem(self, cand: CandidatoArquivo, leitor) -> datetime | None:
        if not cand.fragmentos:
            return None
        try:
            f = cand.fragmentos[0]
            cab = leitor.read(f.offset, min(f.tamanho, 128 * 1024))
        except (aquisicao.ErroLeitura, OSError):
            return None
        tupla = verificacao.data_exif(cab)
        if tupla:
            try:
                cand.data = datetime(*tupla)
                return cand.data
            except (ValueError, TypeError):
                return None
        return None

    # ------------------------------------------------ extração e salvamento

    def _extrair_e_verificar(self, cand: CandidatoArquivo, leitor):
        res = self.resultado
        # duplicidade de região (mesmos bytes já recuperados por outro volume)
        if cand.fragmentos and all(
                self.protegidos.contem(f.offset, f.offset + f.tamanho)
                for f in cand.fragmentos):
            res.duplicados_regiao += 1
            return
        dados, motivo_curto = self._ler_candidato(cand, leitor)
        if dados is None:
            res.recusados.append((cand, motivo_curto or "sem dados legíveis"))
            res.rejeitados += 1
            return
        if motivo_curto:
            cand.estado = ESTADO_PARCIAL
            cand.fracao_recuperada = (len(dados) / cand.tamanho) if cand.tamanho else None
            cand.motivo = (cand.motivo + "; " if cand.motivo else "") + motivo_curto
        self._salvar(cand, dados)

    def _ler_candidato(self, cand: CandidatoArquivo, leitor):
        """Retorna (dados | None, motivo_parcial)."""
        try:
            if cand.extra.get("residente") and "conteudo" in cand.extra:
                return cand.extra["conteudo"], ""
            if cand.extra.get("comprimido") and "runs" in cand.extra:
                fs = self._fs_atual
                if not isinstance(fs, ntfs.SistemaNTFS):
                    return None, "atributo comprimido sem SistemaNTFS disponível"
                rl = ntfs.RunList([])
                for _vcn, runs in cand.extra["runs"]:
                    rl.runs.extend(runs.runs)
                unidade = cand.extra.get("unidade_compressao", 16) or 16
                dados = fs.ler_dados_runlist(rl, cand.tamanho, comprimido=True,
                                             unidade=unidade)
                if dados is None:
                    return None, "falha na descompressão LZNT1"
                if len(dados) < cand.tamanho:
                    return dados, (f"descompressão incompleta "
                                   f"({len(dados)}/{cand.tamanho} bytes)")
                return dados, ""
            esperado = cand.tamanho
            tam_leitor = getattr(leitor, "tamanho", None) or 0
            buf = bytearray()
            for f in cand.fragmentos:
                if self.cancelar.is_set():
                    return None, "cancelado"
                falta = esperado - len(buf)
                if falta <= 0:
                    break
                if f.tamanho <= 0:
                    continue
                pedido = min(f.tamanho, falta)
                if tam_leitor:
                    # o leitor preenche com zeros além do fim físico; não
                    # inventamos dados — a falta vira 'parcial' com fração real
                    disponivel = max(0, tam_leitor - f.offset)
                    if disponivel < pedido:
                        pedido = disponivel
                if pedido <= 0:
                    break
                pedaco = leitor.read(f.offset, pedido)
                if not pedaco:
                    break
                buf += pedaco
            dados = bytes(buf)
            if len(dados) < esperado:
                if not dados:
                    return None, "nenhum byte legível"
                return dados, (f"apenas {len(dados)} de {esperado} bytes "
                               "fisicamente legíveis")
            return dados, ""
        except aquisicao.ErroLeitura as exc:
            self.resultado.erros_leitura += 1
            return None, f"erro de leitura: {exc}"

    def _salvar(self, cand: CandidatoArquivo, dados: bytes):
        op = self.op
        res = self.resultado
        if not dados:
            res.recusados.append((cand, "conteúdo vazio"))
            return
        sha = hashlib.sha256(dados).hexdigest()
        if sha in self._hashes:
            res.duplicados_hash += 1
            return
        for f in cand.fragmentos:
            self.protegidos.adicionar(f.offset, f.offset + f.tamanho)
        self._hashes.add(sha)
        cand.sha256 = sha

        # data via EXIF quando os metadados do FS não a forneceram
        if cand.data is None and cand.extensao in EXTENSOES_FOTO:
            self._data_exif_de_bytes(cand, dados)

        # verificação de integridade
        if cand.estado == ESTADO_PARCIAL:
            pass  # mecanismo real de parcial já aplicado
        elif op.verificar and not op.sem_verificar:
            ver = verificacao.classificar(dados, cand.extensao)
            cand.estado = ver.estado
            cand.fracao_recuperada = ver.fracao
            cand.verificado_por = ver.verificador
            if ver.motivo:
                cand.motivo = (cand.motivo + "; " if cand.motivo else "") + ver.motivo
            if ver.diagnostico:
                cand.extra.setdefault("diagnostico", {}).update(ver.diagnostico)
        else:
            cand.estado = ESTADO_NAO_VERIFICADO
            cand.verificado_por = ""
            cand.motivo = (cand.motivo + "; " if cand.motivo else "") + \
                "verificação desativada pelo usuário"

        # pasta de destino (estrutura original quando conhecida)
        sub = ""
        if not op.sem_pastas and cand.caminho_original and cand.metodo in (
                METODO_PASTA, METODO_MFT, METODO_MFT_ORFA):
            sub = os.path.dirname(cand.caminho_original.replace("\\", os.sep))
            sub = os.path.normpath(sub)
            if sub.startswith("..") or os.path.isabs(sub) or sub == ".":
                sub = ""
        pasta = os.path.join(op.destino, sub) if sub else op.destino
        if cand.estado == ESTADO_CORROMPIDO:
            pasta = os.path.join(op.destino, PASTA_CORROMPIDAS)
        try:
            os.makedirs(pasta, exist_ok=True)
        except OSError as exc:
            res.recusados.append((cand, f"falha ao criar pasta: {exc}"))
            return

        nome = cand.nome
        if not op.sem_data_no_nome and cand.data and cand.metodo == METODO_VARREDURA:
            nome = f"{cand.data:%Y%m%d_%H%M%S}_{nome}"
        if not os.path.splitext(nome)[1]:
            nome += "." + (cand.extensao or "dat")
        nome_final = nome_unico(pasta, nome, self._nomes_usados)
        caminho_final = os.path.join(pasta, nome_final)
        if not os.path.realpath(caminho_final).startswith(
                os.path.realpath(op.destino) + os.sep) and \
                os.path.realpath(caminho_final) != os.path.realpath(op.destino):
            res.recusados.append((cand, "nome de destino fora da pasta validada"))
            return
        try:
            with open(caminho_final, "wb") as f:
                f.write(dados)
        except OSError as exc:
            res.recusados.append((cand, f"falha de gravação: {exc}"))
            return

        cand.arquivo_destino = caminho_final
        res.recuperados += 1
        res.bytes_recuperados += len(dados)
        res.por_estado[cand.estado] = res.por_estado.get(cand.estado, 0) + 1
        res.por_metodo[cand.metodo] = res.por_metodo.get(cand.metodo, 0) + 1
        if len(res.candidatos) < self.MAX_CANDIDATOS_MEMORIA:
            res.candidatos.append(cand)
        self._registrado(f"RECUPERADO {os.path.relpath(caminho_final, op.destino)} "
                         f"({len(dados)} bytes, {cand.metodo}, estado={cand.estado})")

    @staticmethod
    def _data_exif_de_bytes(cand: CandidatoArquivo, dados: bytes):
        tupla = verificacao.data_exif(dados[:128 * 1024])
        if tupla:
            try:
                cand.data = datetime(*tupla)
            except (ValueError, TypeError):
                pass

    # ------------------------------------------------------------- ETAPA 2

    def _etapa2(self):
        op = self.op
        res = self.resultado
        leitor = self.leitor_principal
        if leitor is None:
            self._registrado("Etapa 2 ignorada: nenhuma origem aberta.")
            return
        self._registrado("Etapa 2: varredura por assinatura (file carving)")
        regioes = self._regioes_para_carving(leitor)
        formatos = None
        if not op.videos:
            # HEIC/HEIF/AVIF/CR3 são FOTOGRAFIAS em contêiner ISO-BMFF, então o
            # analisador "mp4" permanece ativo (a escolha entre foto e vídeo é
            # feita pela marca do ftyp e pelo filtro de extensão). Somente os
            # contêineres exclusivamente de vídeo ficam desativados.
            formatos = {"jpeg", "png", "gif", "bmp", "tiff", "raf", "psd",
                        "riff", "mp4"}
        min_bytes = self._min_bytes()
        n = 0
        for ini, fim in regioes:
            if self.cancelar.is_set():
                break
            if fim - ini < 512:
                continue
            for off, resultado in assinaturas.varrer_regiao(
                    leitor, ini, fim, protegido=self.protegidos,
                    cancelar=self.cancelar, min_bytes=min_bytes,
                    formatos=formatos,
                    progresso=lambda f, t, fase: self._fase(0.6 + f * 0.35, fase)):
                if self.cancelar.is_set():
                    break
                if op.limite_arquivos and res.recuperados >= op.limite_arquivos:
                    break
                n += 1
                cand = self._candidato_de_carving(off, resultado, leitor)
                dados = leitor.read(off, cand.tamanho)
                if not dados:
                    continue
                if len(dados) < cand.tamanho:
                    cand.estado = ESTADO_PARCIAL
                    cand.fracao_recuperada = len(dados) / cand.tamanho
                    cand.motivo = (cand.motivo + "; " if cand.motivo else "") + \
                        f"leitura truncada ({len(dados)}/{cand.tamanho})"
                if not self._candidato_passa_filtros(cand):
                    continue
                self._salvar(cand, dados)
        self._registrado(f"Etapa 2 concluída: {n} candidato(s) analisado(s).")

    def _regioes_para_carving(self, leitor):
        """Regiões (absolutas) para a etapa 2. Com 'apenas espaço livre', usa
        os clusters livres dos volumes reconhecidos; caso não haja espaço livre
        mapeável, avisa e usa o volume inteiro (exceto regiões protegidas)."""
        op = self.op
        res = self.resultado
        if op.cartao_inteiro or not self._volumes:
            return [(0, leitor.tamanho)]
        if op.apenas_espaco_livre:
            livres = Intervalos()
            mapeavel = False
            for volume in self._volumes:
                iv = self._espaco_livre_volume(volume)
                if iv is not None:
                    mapeavel = True
                    for a, b in iv:
                        livres.adicionar(a, b)
            if mapeavel and len(livres):
                return [(a, b) for a, b in livres]
            if not mapeavel:
                res.avisos.append("espaço livre não mapeável neste volume; "
                                  "varredura completa (regiões já recuperadas "
                                  "serão ignoradas)")
        regioes = []
        for volume in self._volumes:
            regioes.append((volume["base"], volume["base"] + volume["tamanho"]))
        return regioes or [(0, leitor.tamanho)]

    def _espaco_livre_volume(self, volume: dict) -> Intervalos | None:
        fs = volume["fs"]
        tipo = volume["tipo"]
        iv = Intervalos()
        try:
            if tipo in ("fat16", "fat32"):
                ini_run = None
                for c in range(2, fs.clusters + 2):
                    if self.cancelar.is_set():
                        return None
                    livre = fs.cluster_livre(c)
                    if livre and ini_run is None:
                        ini_run = c
                    elif not livre and ini_run is not None:
                        iv.adicionar(fs.offset_cluster(ini_run),
                                     fs.offset_cluster(c))
                        ini_run = None
                if ini_run is not None:
                    iv.adicionar(fs.offset_cluster(ini_run),
                                 fs.dados_off + fs.clusters * fs.cluster_bytes)
            elif tipo == "exfat":
                fs.carregar_bitmap()
                bm = fs._bitmap_bytes
                if not bm:
                    return None
                ini_run = None
                for c in range(2, fs.n_clusters + 2):
                    idx = c - 2
                    livre = not (bm[idx // 8] & (1 << (idx % 8))) if idx // 8 < len(bm) else True
                    if livre and ini_run is None:
                        ini_run = c
                    elif not livre and ini_run is not None:
                        iv.adicionar(fs.offset_cluster(ini_run), fs.offset_cluster(c))
                        ini_run = None
                if ini_run is not None:
                    iv.adicionar(fs.offset_cluster(ini_run), fs.fim_heap)
            elif tipo == "ntfs":
                # varre o $Bitmap em blocos de 64 KiB
                total_clusters = (fs.vol_tam + fs.cluster_bytes - 1) // fs.cluster_bytes
                ini_run = None
                base = 0
                passo_bytes = 65536
                while base * 8 < total_clusters:
                    if self.cancelar.is_set():
                        return None
                    bloco = fs._ler_bitmap_bytes(base, passo_bytes)
                    if bloco is None:
                        return None
                    for byte_i, byte in enumerate(bloco):
                        for bit in range(8):
                            lcn = base * 8 + byte_i * 8 + bit
                            if lcn >= total_clusters:
                                break
                            livre = not (byte & (1 << bit))
                            if livre and ini_run is None:
                                ini_run = lcn
                            elif not livre and ini_run is not None:
                                iv.adicionar(fs.offset_cluster(ini_run),
                                             fs.offset_cluster(lcn))
                                ini_run = None
                    if ini_run is not None and len(bloco) < passo_bytes:
                        iv.adicionar(fs.offset_cluster(ini_run),
                                     fs.offset_cluster(total_clusters))
                        ini_run = None
                    base += passo_bytes
                if ini_run is not None:
                    iv.adicionar(fs.offset_cluster(ini_run),
                                 fs.offset_cluster(total_clusters))
            else:
                return None
        except Exception:
            return None
        return iv if len(iv) else None

    def _candidato_de_carving(self, off: int, resultado, leitor) -> CandidatoArquivo:
        ext = resultado.extensao
        nome = f"carv_{off:012d}.{ext}"
        comp = resultado.comprimento
        diag = resultado.diagnostico or {}
        motivo = resultado.motivo
        truncado = resultado.truncado

        # JPEG MPF: páginas contíguas e índices válidos => arquivo completo
        mpf = diag.get("mpf")
        if mpf and mpf.get("valido") and mpf.get("imagens"):
            fim_mpf = max(o + t for o, t in mpf["imagens"])
            if fim_mpf > off + comp:
                contiguas = True
                for o, _t in mpf["imagens"][1:]:
                    if leitor.read(o, 3) != b"\xFF\xD8\xFF":
                        contiguas = False
                        break
                if contiguas:
                    comp = fim_mpf - off
                    motivo = (motivo + "; " if motivo else "") + \
                        "MPF: índices válidos, páginas contíguas — arquivo completo"
                else:
                    motivo = (motivo + "; " if motivo else "") + \
                        "MPF: junção recusada (páginas não verificáveis)"
                    diag["mpf_recusado"] = True
            else:
                motivo = (motivo + "; " if motivo else "") + \
                    "MPF: índice presente, mas sem páginas além da primária"
        elif mpf and not mpf.get("valido"):
            diag["mpf_recusado"] = True
            motivo = (motivo + "; " if motivo else "") + \
                "MPF: índice inválido (" + (mpf.get("motivo") or "inconsistente") + \
                ") — somente a imagem primária foi recuperada"

        # Motion Photo: MP4 anexo logo após o EOI
        mv = diag.get("motion_video_offset")
        if mv and mv > 0:
            fim_jpeg = off + comp
            regiao = leitor.read(fim_jpeg, 16)
            if len(regiao) >= 12 and regiao[4:8] == b"ftyp":
                res2 = assinaturas.analisar_mp4(leitor, fim_jpeg, fim_regiao=None)
                if res2 and res2.comprimento and off + mv + res2.comprimento >= fim_jpeg:
                    comp += res2.comprimento
                    motivo = (motivo + "; " if motivo else "") + \
                        "Motion Photo: vídeo MP4 anexo verificável preservado " \
                        "(não extraído como duplicata)"

        cand = CandidatoArquivo(
            nome, [Fragmento(off, comp)], METODO_VARREDURA,
            resultado.formato, caminho_original="",
            tamanho=comp, data=None,
            confiavel=not truncado, motivo=motivo,
            extra={"diagnostico": diag})
        return cand

    # ------------------------------------------------------------ relatórios

    def _gerar_relatorios(self):
        res = self.resultado
        destino = self.op.destino
        if not destino or not os.path.isdir(destino):
            return
        caminho_csv = os.path.join(destino, "relatorio_recuperacao.csv")
        try:
            with open(caminho_csv, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f, delimiter=";")
                w.writerow(COLUNAS_CSV)
                for c in res.candidatos:
                    w.writerow([
                        os.path.basename(c.arquivo_destino or c.nome),
                        c.extensao,
                        "assinatura" if c.metodo == METODO_VARREDURA else "metadados",
                        c.caminho_original or "",
                        c.posicao_no_cartao,
                        c.tamanho if c.tamanho is not None else "",
                        c.data.strftime("%d/%m/%Y %H:%M:%S") if c.data else "",
                        c.estado or "",
                        f"{c.fracao_recuperada:.4f}".replace(".", ",")
                        if c.fracao_recuperada is not None else "",
                        c.metodo,
                        c.motivo or "",
                    ])
                for c, motivo in res.recusados:
                    w.writerow([
                        c.nome, c.extensao,
                        "assinatura" if c.metodo == METODO_VARREDURA else "metadados",
                        c.caminho_original or "",
                        c.posicao_no_cartao,
                        c.tamanho if c.tamanho is not None else "",
                        c.data.strftime("%d/%m/%Y %H:%M:%S") if c.data else "",
                        "recusado", "",
                        c.metodo, motivo,
                    ])
            res.relatorio_csv = caminho_csv
        except OSError as exc:
            res.avisos.append(f"falha ao gravar o relatório: {exc}")

        if res.recusas_ntfs:
            caminho_diag = os.path.join(destino, "diagnostico_ntfs.csv")
            try:
                with open(caminho_diag, "w", encoding="utf-8-sig", newline="") as f:
                    w = csv.writer(f, delimiter=";")
                    w.writerow(COLUNAS_DIAG)
                    vistos = set()
                    for r in res.recusas_ntfs:
                        chave = (str(r["registro_MFT"]), r["motivo"])
                        if chave in vistos:
                            continue
                        vistos.add(chave)
                        w.writerow([r["registro_MFT"], r.get("nome", ""), r["motivo"]])
                res.diagnostico_csv = caminho_diag
            except OSError as exc:
                res.avisos.append(f"falha ao gravar o diagnóstico NTFS: {exc}")
