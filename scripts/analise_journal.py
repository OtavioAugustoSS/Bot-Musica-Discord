"""Análise do journal do Arrodes: voz, erros, saúde, faixas.

Como usar (do PC, com o alias ssh `arrodes`):

    ssh arrodes 'journalctl -u arrodes _SYSTEMD_INVOCATION_ID=$(systemctl show \
        -p InvocationID --value arrodes) -o short-iso --no-pager' > journal.log
    python scripts/analise_journal.py journal.log [outro.log ...]

Lê o formato `short-iso` (horário em UTC, mostrado em BRT) e responde às
perguntas que o estudo de 04/10/2026 precisou fazer à mão: quantas chamadas a
2ª escuta barrou e se a pessoa repetiu depois; quantos trechos acima do teto
não foram para a Groq e de quem; quantos 429 e perdidos; a surdez por falha de
descriptografia (AEAD em rajada) cruzada com as transcrições por usuário; as
travadas do event loop; e o resumo das faixas e da linha `saúde:`.

E a colheita das grafias do nome: as linhas "sem wake word" dizem a 1ª palavra
e a grafia que mais lembrou o nome (core/intents.py::inicio_para_log), e as
que terminam em "o resto leria X" são chamadas caladas — o nome saiu de um
jeito que o parser não aceita. O histograma delas é a lista de candidatas a
_WAKE_VARIANTS (scripts/calibra_wake.py confere com áudio de verdade).
"""
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")  # o console do Windows é cp1252

LINHA = re.compile(
    r"^(\S+) \S+ \S+\[\d+\]: (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (\w+) ([\w.]+): (.*)$"
)
BRT = timezone(timedelta(hours=-3))
# "[7] sem wake word (começa com rode→'rode' 0.73), o resto leria skip"; sem nada
# parecido com o nome, "(começa com cara 0.46)"; e o formato antigo, "(começa com 'cara')"
SEM_WAKE = re.compile(
    r"\[(-?\d+)\] sem wake word \(começa com (?:'(?P<velho>[^']*)'|(?P<primeira>[^\s→)]+)"
    r"(?:→'(?P<grafia>[^']*)')?(?: (?P<nota>\d\.\d\d))?)\)(?:, o resto leria (?P<acao>\w+))?"
)
# "[7] 'Arrodes' só na 1ª escuta (que leu skip) — a 2ª escuta (sem o nome no prompt)
# começa com 'entao', 2ª leu unknown: ignorado" — o nome entre aspas é o WAKE_WORD
BARRADA = re.compile(
    r"\[(-?\d+)\] '[^']+' só na 1ª escuta (?:\(que leu (\S+)\) )?— a 2ª escuta"
    r" \(sem o nome no prompt\) começa com '(.*?)'(?:, 2ª leu (\S+))?: ignorado"
)
VERBOS = {
    "pula", "pule", "proxima", "proximo", "toca", "toque", "para", "pausa", "pausar", "volta",
    "continua", "aumenta", "abaixa", "diminui", "coloca", "bota", "poe", "tira", "limpa",
    "embaralha", "sai", "repete", "vai", "passa", "skip", "next", "troca", "volume", "avanca",
    "desliga", "muda", "pare", "cala", "fica",
}


def carrega(caminho):
    evs = []
    with open(caminho, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            m = LINHA.match(raw)
            if not m:
                evs.append((None, None, "RAW", "", raw))
                continue
            # o rel\u00f3gio do bot na VM \u00e9 UTC; mostramos em BRT
            ts = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).astimezone(BRT)
            msg = m.group(5).removeprefix("\U0001f399\ufe0f ")
            evs.append((ts, m.group(1), m.group(3), m.group(4), msg))
    return evs


def h(ts):
    return ts.strftime("%d/%m %H:%M:%S") if ts else "--"


def chave(msg):
    return re.sub(r"\d+(\.\d+)?", "N", msg)[:110]


def le_sem_wake(msg):
    """Linha "sem wake word" → dict (uid, primeira, grafia, nota, acao), ou None."""
    m = SEM_WAKE.search(msg)
    if not m:
        return None
    return {
        "uid": int(m.group(1)),
        "primeira": m.group("velho") if m.group("velho") is not None else m.group("primeira"),
        "grafia": m.group("grafia"),
        "nota": float(m.group("nota")) if m.group("nota") else None,
        "acao": m.group("acao"),
    }


def pct(xs, p):
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))]


def analisa(caminho):
    evs = carrega(caminho)
    com_ts = [e for e in evs if e[0]]
    if not com_ts:
        print(f"ARQUIVO {caminho}: nenhuma linha no formato short-iso")
        return
    print("=" * 100)
    print(f"ARQUIVO {caminho}: {len(evs)} linhas, {h(com_ts[0][0])} -> {h(com_ts[-1][0])} (BRT)")
    print("=" * 100)

    for nivel in ("ERROR", "CRITICAL", "WARNING"):
        grupos = defaultdict(list)
        for ts, _, lvl, mod, msg in evs:
            if lvl == nivel:
                grupos[(mod, chave(msg))].append(ts)
        if grupos:
            print(f"\n--- {nivel}: {sum(len(v) for v in grupos.values())} linhas em {len(grupos)} grupos")
            for (mod, k), tss in sorted(grupos.items(), key=lambda kv: -len(kv[1])):
                print(f"  {len(tss):4d}  {mod:18s} {k}   [{h(tss[0])} ... {h(tss[-1])}]")

    print("\n--- event loop travou:")
    for ts, _, lvl, mod, msg in evs:
        if ts and "travou" in msg:
            print(f"  {h(ts)} {msg[:230]}")

    aead = []
    for ts, _, lvl, mod, msg in evs:
        if ts and "AEAD" in msg:
            m = re.search(r"\+(\d+) iguais", msg)
            aead.append((ts, 1 + (int(m.group(1)) if m else 0)))
    if aead:
        pacotes = sum(n for _, n in aead)
        print(f"\n--- AEAD (descriptografia falhou): {len(aead)} avisos, ~{pacotes} pacotes"
              f" (~{pacotes * 0.02 / 60:.0f} min de áudio a 50 pacotes/s), de {h(aead[0][0])} a {h(aead[-1][0])}")
    for ts, _, lvl, mod, msg in evs:
        if ts and "chave da conexão mudou" in msg:
            print(f"  {h(ts)} {msg[:160]}")

    voz = [(ts, mod, msg) for ts, _, lvl, mod, msg in evs
           if ts and mod in ("core.listen", "services.speech", "services.llm")]
    transc = []
    for ts, mod, msg in voz:
        m = re.search(r"\[(-?\d+)\] transcrito em ([\d.]+)s \((\d+) chars, ([\d.]+)s de fala aparada,"
                      r" (\d+) janelas com voz, comando=(\S+)\)", msg)
        if m:
            transc.append((ts, int(m.group(1)), float(m.group(2)), int(m.group(3)),
                           float(m.group(4)), int(m.group(5)), m.group(6)))
    print(f"\n--- transcrições: {len(transc)}")
    if transc:
        el = [t[2] for t in transc]
        print(f"  latência Groq: p50 {pct(el, .5):.2f}s p90 {pct(el, .9):.2f}s p99 {pct(el, .99):.2f}s max {max(el):.2f}s")
        dur = [t[4] for t in transc]
        print(f"  fala aparada: p50 {pct(dur, .5):.1f}s p90 {pct(dur, .9):.1f}s max {max(dur):.1f}s")
        print(f"  comandos que passaram (comando= na linha transcrito): {sum(1 for t in transc if t[6] != '-')}")
        print(f"  por ação: {Counter(t[6] for t in transc if t[6] != '-').most_common()}")
        print(f"  transcrições vazias (0 chars): {sum(1 for t in transc if t[3] == 0)}")
        tab = defaultdict(Counter)
        for t in transc:
            tab[t[1]][t[0].strftime("%d/%m %Hh")] += 1
        horas = sorted({k for c in tab.values() for k in c})
        print("  transcrições por usuário x hora:")
        print("    " + " " * 20 + " ".join(f"{x:>9}" for x in horas))
        for uid, c in sorted(tab.items(), key=lambda kv: -sum(kv[1].values())):
            print(f"    {uid:>20} " + " ".join(f"{c.get(x, 0):9d}" for x in horas))
        print("  por usuário: trechos, acima do teto não enviados (0 chars e >6s), estourando 12s:")
        por_uid = defaultdict(lambda: [0, 0, 0])
        for t in transc:
            por_uid[t[1]][0] += 1
            if t[3] == 0 and t[4] > 6.0:
                por_uid[t[1]][1] += 1
            if t[4] >= 11.5:
                por_uid[t[1]][2] += 1
        for uid, (n, longos, doze) in sorted(por_uid.items(), key=lambda kv: -kv[1][0]):
            print(f"    {uid:>20} trechos={n:5d} >6s descartados={longos:4d} ({100 * longos / n:4.1f}%)  12s={doze:4d}")

    if aead:
        inicio = aead[0][0] - timedelta(minutes=20)
        fim = aead[-1][0] + timedelta(minutes=10)
        tab = defaultdict(Counter)
        for t in transc:
            if inicio <= t[0] <= fim:
                tab[t[1]][t[0].strftime("%d %H:%M")[:-1] + "0"] += 1
        janelas = sorted({k for c in tab.values() for k in c})
        if janelas:
            print("\n--- transcrições por usuário x 10min ao redor das falhas de AEAD:")
            print("    " + " " * 20 + " ".join(f"{x[-5:]:>6}" for x in janelas))
            for uid, c in sorted(tab.items(), key=lambda kv: -sum(kv[1].values())):
                print(f"    {uid:>20} " + " ".join(f"{c.get(x, 0):6d}" for x in janelas))
            ae = Counter()
            for ts, n in aead:
                ae[ts.strftime("%d %H:%M")[:-1] + "0"] += n
            print(f"    {'AEAD pacotes':>20} " + " ".join(f"{ae.get(x, 0):6d}" for x in janelas))

    sem_wake = [sw for _, _, msg in voz if (sw := le_sem_wake(msg))]
    swk = Counter(sw["primeira"] for sw in sem_wake)
    total_swk = sum(swk.values())
    eco = sum(v for k, v in swk.items() if k in ("musicas", "musica"))
    aluc = sum(v for k, v in swk.items()
               if k in ("subtitles", "thank", "thanks", "legendas", "obrigado", "tchau", "amara", "www"))
    verbos = Counter({k: v for k, v in swk.items() if k in VERBOS})
    print(f"\n--- sem wake word: {total_swk}; eco do prompt 'Músicas' = {eco}"
          f" ({100 * eco / max(1, total_swk):.0f}%); alucinações (subtitles/thank/tchau...) = {aluc}")
    print(f"  começando por VERBO de comando: {sum(verbos.values())} -> {verbos.most_common()}")
    print(f"  1ª palavra (top 30): {swk.most_common(30)}")

    # a colheita: como o Whisper escreveu o nome quando o parser não o aceitou
    grafias = Counter(sw["grafia"] for sw in sem_wake if sw["grafia"])
    nota_da = {sw["grafia"]: sw["nota"] or 0.0 for sw in sem_wake if sw["grafia"]}
    print(f"\n--- grafias que lembram o nome (nota >= 0.6) nas linhas sem wake word: {sum(grafias.values())}"
          f" em {len(grafias)} grafias — candidatas a _WAKE_VARIANTS (confira com scripts/calibra_wake.py):")
    for grafia, n in grafias.most_common(40):
        print(f"  {n:5d}  {grafia:20s} {nota_da[grafia]:.2f}")
    notas = [sw["nota"] for sw in sem_wake if sw["nota"] is not None]
    if notas:
        faixas = Counter(min(int(n * 10), 9) / 10 for n in notas)
        print("  notas contra o nome (todas as linhas): "
              + " ".join(f"{f:.1f}+:{faixas[f]}" for f in sorted(faixas)))
    caladas = [sw for sw in sem_wake if sw["acao"]]
    print(f"\n--- 'o resto leria' (chamada calada — sem a 1ª palavra a frase era comando): {len(caladas)}")
    if caladas:
        print(f"  por ação: {Counter(sw['acao'] for sw in caladas).most_common()}")
        print(f"  por grafia (ou 1ª palavra): "
              f"{Counter(sw['grafia'] or sw['primeira'] for sw in caladas).most_common(30)}")

    poup_fixo = poup_min = 0
    durs_fixo, durs_min = [], []
    for ts, mod, msg in voz:
        m = re.search(r"trecho de ([\d.]+)s não foi para a Groq: acima do teto", msg)
        if m:
            poup_fixo += 1
            durs_fixo.append(float(m.group(1)))
            continue
        m = re.search(r"trecho de ([\d.]+)s não foi para a Groq: (\d+) de (\d+) requisições", msg)
        if m:
            poup_min += 1
            durs_min.append(float(m.group(1)))
    print(f"\n--- poupados: teto fixo {poup_fixo} (dur p50 {pct(durs_fixo, .5):.1f}s, p10 {pct(durs_fixo, .1):.1f}s);"
          f" minuto apertado {poup_min} (dur p50 {pct(durs_min, .5):.1f}s)")
    n429 = sum(1 for _, _, msg in voz if "devolveu 429" in msg)
    perd = sum(1 for _, _, msg in voz if "comando perdido" in msg)
    cortes = sum(1 for _, _, msg in voz if "cortado na pausa" in msg)
    print(f"  avisos de 429: {n429}; 'comando perdido': {perd}; cortados na pausa (1ª frase foi): {cortes}")

    ru = []
    for ts, mod, msg in voz:
        m = re.search(r"trecho de ([\d.]+)s descartado como ruído \((\d+) janelas com voz, pico RMS (\d+),"
                      r" limiar VOICE_MIN_RMS=(\d+)", msg)
        if m:
            ru.append((float(m.group(1)), int(m.group(2)), int(m.group(3))))
    if ru:
        print(f"\n--- descartados como ruído (INFO): {len(ru)}; com 2 janelas: {sum(1 for r in ru if r[1] == 2)};"
              f" pico >= limiar: {sum(1 for r in ru if r[2] >= 250)}; duração p50 {pct([r[0] for r in ru], .5):.1f}s")

    por_user = defaultdict(list)
    for ts, mod, msg in voz:
        m = re.match(r"\[(-?\d+)\] (.*)", msg)
        if m:
            por_user[int(m.group(1))].append((ts, m.group(2)))

    def contexto(uid, ts, antes=12, depois=25):
        saida = []
        for t2, m2 in por_user.get(uid, []):
            d = (t2 - ts).total_seconds()
            if -antes <= d <= depois and d != 0:
                saida.append(f"        {d:+6.1f}s {m2[:170]}")
        return saida

    print("\n--- COMANDOS DE VOZ atendidos (e o que o mesmo usuário fez nos 15s ANTES):")
    for ts, mod, msg in voz:
        m = re.match(r"\[(-?\d+)\] comando de voz (.*)", msg)
        if m:
            print(f"  {h(ts)} [{m.group(1)}] {m.group(2)[:200]}")
            for linha in contexto(int(m.group(1)), ts, antes=15, depois=0):
                if "transcrito" not in linha or "0 chars" in linha:
                    print(linha)

    print("\n--- sem wake word começando por verbo (menos vai/para), com contexto -10s..+10s:")
    for ts, mod, msg in voz:
        sw = le_sem_wake(msg)
        if sw and sw["primeira"] in VERBOS - {"vai", "para"}:
            print(f"  {h(ts)} [{sw['uid']}] começa com {sw['primeira']!r}")
            for linha in contexto(sw["uid"], ts, antes=10, depois=10):
                print(linha)

    longos = [t for t in transc if t[3] == 0 and t[4] > 6.0]
    seguidos = 0
    for t in longos:
        for t2, m2 in por_user.get(t[1], []):
            d = (t2 - t[0]).total_seconds()
            if 0 < d <= 15 and ("comando de voz" in m2 or "só na 1ª escuta" in m2):
                seguidos += 1
                break
    print(f"\n--- trechos >6s não enviados (0 chars): {len(longos)}; seguidos em 15s por chamada do mesmo usuário: {seguidos}")

    print("\n--- LLM / conversa:")
    for ts, mod, msg in voz:
        if ("LLM leu" in msg or "quis tocar" in msg or "era conversa" in msg
                or "já está na fila" in msg or "sem resultado" in msg):
            print(f"  {h(ts)} {msg[:260]}")

    print("\n--- BARRADAS pela 2a escuta (e o que o mesmo usuário fez em -12s..+25s):")
    for ts, mod, msg in voz:
        m = BARRADA.match(msg)
        if m:
            uid = int(m.group(1))
            print(f"  {h(ts)} [{uid}] 1a leu {(m.group(2) or '?'):10s} 2a começa com {m.group(3)!r}"
                  f" (2a leu {m.group(4) or '?'})")
            for linha in contexto(uid, ts):
                print(linha)

    print("\n--- hello (chamou sem comando) e o que veio depois:")
    for ts, mod, msg in voz:
        if re.match(r"\[(-?\d+)\] comando de voz hello", msg):
            uid = int(re.match(r"\[(-?\d+)\]", msg).group(1))
            print(f"  {h(ts)} [{uid}] hello")
            for linha in contexto(uid, ts, antes=0, depois=15):
                print(linha)

    print("\n--- escuta/voz:")
    for ts, _, lvl, mod, msg in evs:
        if ts and ("escuta ligada" in msg or "escuta desligada" in msg or "bot na voz" in msg
                   or "entrei em" in msg or "parando (" in msg or "conexão de voz" in msg
                   or "Disconnected from voice" in msg):
            print(f"  {h(ts)} {msg[:160]}")

    saude = [(ts, msg) for ts, _, lvl, mod, msg in evs if ts and "saúde:" in msg]
    print(f"\n--- saúde: {len(saude)} linhas; últimas 2 e as 2 com mais steal:")
    for ts, msg in saude[-2:]:
        print(f"  {h(ts)} {msg[:400]}")

    def steal(msg):
        m = re.search(r"steal (\d+)%", msg)
        return int(m.group(1)) if m else -1

    for ts, msg in sorted(saude, key=lambda x: -steal(x[1]))[:2]:
        print(f"  {h(ts)} {msg[:400]}")
    for rotulo in ("groq", "escuta"):
        tot = Counter()
        for ts, msg in saude:
            m = re.search(rotulo + r" ([^|]*)", msg)
            if m:
                for k, v in re.findall(r"(\w+)=(\d+)", m.group(1)):
                    tot[k] += int(v)
        print(f"  soma dos contadores {rotulo}: {dict(tot)}")
    lags = []
    for ts, msg in saude:
        m = re.search(r"lag máx do loop ([\d.]+)s", msg)
        if m:
            lags.append(float(m.group(1)))
    if lags:
        print(f"  lag máx do loop: p50 {pct(lags, .5):.2f}s p90 {pct(lags, .9):.2f}s max {max(lags):.2f}s")
    real = []
    for ts, msg in saude:
        m = re.search(r"áudio realinhado (\d+)x \(maior atraso (\d+)ms\)", msg)
        if m:
            real.append((int(m.group(1)), int(m.group(2))))
    if real:
        print(f"  áudio realinhado: {sum(r[0] for r in real)}x; maior atraso p50 {pct([r[1] for r in real], .5):.0f}ms"
              f" max {max(r[1] for r in real)}ms; >500ms em {sum(1 for r in real if r[1] > 500)} janelas")

    fins = [(ts, msg) for ts, _, lvl, mod, msg in evs if ts and re.search(r"fim de .*: tocou", msg)]
    cat = Counter()
    recon = 0
    for ts, msg in fins:
        m = re.search(r"\((acabou|pulada|parada|MORREU NA LARGADA|CORTADA antes do fim)\)", msg)
        if m:
            cat[m.group(1)] += 1
        m = re.search(r"CDN reconectou (\d+)x", msg)
        if m:
            recon += int(m.group(1))
    print(f"\n--- faixas: {len(fins)} fins; {dict(cat)}; reconexões CDN total {recon}")
    for ts, msg in fins:
        if "faltou" in msg or "MORREU" in msg or "CORTADA" in msg or "recusa" in msg:
            print(f"  {h(ts)} {msg[:220]}")

    reg = Counter()
    dur_cmd = defaultdict(list)
    for ts, _, lvl, mod, msg in evs:
        if mod == "cogs.registro":
            m = re.match(r"(?:\[\d+\] )?(/\w+|clique em [\w:]+)", msg)
            if m:
                reg[m.group(1)] += 1
            m = re.match(r"(?:\[\d+\] )?(/\w+) terminou em ([\d.]+)s", msg)
            if m:
                dur_cmd[m.group(1)].append(float(m.group(2)))
    print(f"\n--- registro: {reg.most_common(15)}")
    for c, ds in dur_cmd.items():
        print(f"  {c}: n={len(ds)} p50 {pct(ds, .5):.1f}s max {max(ds):.1f}s")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    for caminho in sys.argv[1:]:
        analisa(caminho)
