import React, { useState, useEffect } from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Award,
  BarChart3,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  Code2,
  Cpu,
  Database,
  Eye,
  FileCheck,
  FileSpreadsheet,
  FileText,
  Filter,
  GitBranch,
  HelpCircle,
  Layers,
  Lock,
  Play,
  RefreshCw,
  Scale,
  Search,
  Server,
  Settings,
  ShieldAlert,
  ShieldCheck,
  Sliders,
  Sparkles,
  Target,
  Terminal,
  TrendingUp,
  Users,
  XCircle,
  Zap
} from 'lucide-react';

// --- PALETTE ISPIRATA ALLE SLIDE DI SCALE AI ---
// Warm off-white (#F6F5F0), Olive/Sage (#767E6E), Deep Slate (#1C2024)

export default function App() {
  const [activeTab, setActiveTab] = useState('workflow');
  const [darkMode, setDarkMode] = useState(false);

  // --- STATI TAB 1: WORKFLOW & ROI SIMULATOR (Slide 1-3) ---
  const [workflowStep, setWorkflowStep] = useState(0);
  const [isLoopingEvidence, setIsLoopingEvidence] = useState(false);
  const [isLoopingRevision, setIsLoopingRevision] = useState(false);
  const [packagesPerMonth, setPackagesPerMonth] = useState(20);
  const [baselineHours, setBaselineHours] = useState(20);
  const [targetHours, setTargetHours] = useState(8);
  const [reworkOverhead, setReworkOverhead] = useState(1.5); // ore extra se il grader/agent sbaglia

  // --- STATI TAB 2: 5 CRITERI, GATE & LIVE CODE (Slide 4-10, 17) ---
  const [criteriaState, setCriteriaState] = useState({
    C1: true,
    C2: false, // Impostato a false di default per mostrare il caso della Slide 17 (C1 Pass, C2 Fail -> FAIL)
    C3: true,
    C4: true,
    C5: true
  });
  const [selectedCriterion, setSelectedCriterion] = useState('C1');
  const [inspectorTab, setInspectorTab] = useState('c1_code');
  // Simulator C1 Python Verifier
  const [draftReported, setDraftReported] = useState(10.0);
  const [draftAdj, setDraftAdj] = useState(5.2);
  const [draftFinal, setDraftFinal] = useState(15.2);
  const [tol, setTol] = useState(0.05);
  const refReported = 10.0;
  const refAdj = 5.2;
  const refFinal = 15.2;

  // --- STATI TAB 3: BENCHMARK, GRADER TRAP & STATISTICA (Slide 11-16, 18) ---
  const [graderPreset, setGraderPreset] = useState('lenient'); // 'lenient' (Slide 15), 'strict', 'calibrated'
  const [badDraftsPassed, setBadDraftsPassed] = useState(10); // su 10
  const [goodDraftsFailed, setGoodDraftsFailed] = useState(0); // su 90
  const [sampleSize, setSampleSize] = useState(400); // 20, 100, 300, 400

  // --- STATI TAB 4: PRODUZIONE & DASHBOARD (Slide 19-24) ---
  const [prodViewMode, setProdViewMode] = useState('both'); // 'offline', 'online', 'both'
  const [checkedGaps, setCheckedGaps] = useState({
    gap1: true,
    gap2: false,
    gap3: false,
    gap4: false,
    gap5: false
  });

  // Animazione automatica del workflow
  useEffect(() => {
    const timer = setInterval(() => {
      setWorkflowStep((prev) => (prev + 1) % 4);
    }, 3200);
    return () => clearInterval(timer);
  }, []);

  // Gestione preset Grader Trap (Slide 15)
  const applyGraderPreset = (preset) => {
    setGraderPreset(preset);
    if (preset === 'lenient') {
      // Slide 15 esatta: passa tutti i 100 draft -> 90% agreement, ma 10/10 bad drafts passed!
      setBadDraftsPassed(10);
      setGoodDraftsFailed(0);
    } else if (preset === 'strict') {
      // Troppo severo: blocca quasi tutti i bad, ma boccia molti good
      setBadDraftsPassed(1);
      setGoodDraftsFailed(18);
    } else if (preset === 'calibrated') {
      // Calibrato sugli esperti
      setBadDraftsPassed(1);
      setGoodDraftsFailed(2);
    }
  };

  // Calcoli ROI (Slide 1)
  const grossHoursSavedPerPkg = Math.max(0, baselineHours - targetHours);
  const netHoursSavedPerPkg = Math.max(0, baselineHours - (targetHours + reworkOverhead));
  const totalMonthlyHoursSaved = Math.round(packagesPerMonth * grossHoursSavedPerPkg);
  const totalNetMonthlyHours = Math.round(packagesPerMonth * netHoursSavedPerPkg);

  // Calcoli Python Verifier C1 (Slide 8)
  const c1Checks = {
    reported_ebitda: Math.abs(draftReported - refReported) <= tol,
    adjustment_total: Math.abs(draftAdj - refAdj) <= tol,
    adjusted_ebitda: Math.abs(draftFinal - refFinal) <= tol,
    reconciles: Math.abs(draftReported + draftAdj - draftFinal) <= tol
  };
  const c1AllPass = Object.values(c1Checks).every(Boolean);

  // Calcoli Criteri C1-C5 (Slide 10 & 17)
  const allFivePass = Object.values(criteriaState).every(Boolean);

  // Calcoli Grader Validation (Slide 15)
  const correctGood = 90 - goodDraftsFailed;
  const correctBad = 10 - badDraftsPassed;
  const overallAgreement = correctGood + correctBad; // su 100

  // Calcoli Significatività Statistica (Slide 18)
  // Pass rate illustrativo = 95%
  const p = 0.95;
  const z = 1.96;
  const se = Math.sqrt((p * (1 - p)) / sampleSize);
  const ciLow = Math.max(0, Math.round((p - z * se) * 100));
  const ciHigh = Math.min(100, Math.round((p + z * se) * 100));
  // Regola del 3 (Rule of 3) per zero errori osservati: 95% upper bound ≈ 3 / N
  const zeroErrorUpperBound = ((3 / sampleSize) * 100).toFixed(1);

  // Dati dettagliati dei 5 Criteri (Slide 4, 5, 6, 10, 20)
  const criteriaData = {
    C1: {
      id: 'C1',
      name: 'Numeric accuracy',
      titleIt: 'Accuratezza Numerica',
      whatMustBeTrue: 'Cifre e calcoli sono corretti',
      ddExample: 'Riconciliazione EBITDA bridge e totali',
      groundTruth: 'QoE bridge + financial databook (es. riconciliare $5.2m alla fonte)',
      variability: '1. Valori fissi e tolleranze dichiarate',
      variabilityRule: 'Stesso periodo, valuta e unità entro tolleranza numerica.',
      graderType: 'Deterministic Check (Codice Python)',
      verifierDesc: 'Codice vs. riferimento + test di riconciliazione matematica',
      outputResult: 'PASS / FAIL + differenze numeriche',
      onlineScope: 'Ricalcola il bridge; verifica corrispondenza input con la VDR',
      outOfScopeOnline: 'Verifica contro l\'EBITDA bridge validato dall\'esperto'
    },
    C2: {
      id: 'C2',
      name: 'Completeness',
      titleIt: 'Completezza dei Finding',
      whatMustBeTrue: 'Compaiono tutti i finding materiali richiesti',
      ddExample: 'Rettifiche (adjustments), rischi materiali e follow-up',
      groundTruth: 'Report findings + risk summary (catturare rettifiche e richieste aperte)',
      variability: '2. Espressione equivalente ammessa',
      variabilityRule: 'Accetta formulazioni o strutture diverse se tutti i finding materiali sono preservati.',
      graderType: 'Model-based Judge (LLM con Rubric)',
      verifierDesc: 'Giudice LLM vs. finding materiali richiesti nella ground truth',
      outputResult: 'PASS / FAIL + elenco omissioni',
      onlineScope: 'Controlla presenza delle sezioni richieste e argomenti in scope',
      outOfScopeOnline: 'Confermare che ogni singolo rischio/finding materiale sia stato coperto'
    },
    C3: {
      id: 'C3',
      name: 'Source support',
      titleIt: 'Supporto delle Fonti (Evidenze)',
      whatMustBeTrue: 'Ogni affermazione materiale ha prove valide',
      ddExample: 'Le citazioni identificano il documento o la cella esatta nella VDR',
      groundTruth: 'Citazioni + workpaper collegati (validare tutti i link alle prove)',
      variability: '2. Espressione equivalente ammessa',
      variabilityRule: 'Il testo può variare ma il legame con l\'evidenza documentale deve essere solido.',
      graderType: 'Hybrid: Citation Checks + Evidence Judge',
      verifierDesc: 'Controllo citazioni + Giudice LLM sulle fonti VDR',
      outputResult: 'PASS / FAIL + affermazioni non supportate',
      onlineScope: 'Verifica che i documenti/fogli VDR citati supportino le affermazioni',
      outOfScopeOnline: 'Nessuno: il supporto delle fonti resta interamente verificabile online!'
    },
    C4: {
      id: 'C4',
      name: 'Analytical judgment',
      titleIt: 'Giudizio Analitico',
      whatMustBeTrue: 'Le conclusioni seguono i criteri concordati',
      ddExample: 'Il trattamento di ogni add-back all\'EBITDA è giustificato',
      groundTruth: 'Note sulle rettifiche + commenti di revisione (es. accetta +$0.4m, escludi $0.6m)',
      variability: '3. Alternative accettate definite da esperti',
      variabilityRule: 'Registra trattamenti difendibili; gli esperti definiscono quali alternative soddisfano i criteri.',
      graderType: 'Model-based Judge + Expert Calibration',
      verifierDesc: 'Rubric vs. trattamenti accettati dagli esperti',
      outputResult: 'PASS / FAIL + motivazione (rationale)',
      onlineScope: 'Controlla la logica rispetto alla policy standard di add-back',
      outOfScopeOnline: 'Confermare il trattamento specifico accettato dall\'esperto per quel deal'
    },
    C5: {
      id: 'C5',
      name: 'Uncertainty handling',
      titleIt: 'Gestione dell\'Incertezza',
      whatMustBeTrue: 'Le lacune nelle prove (evidence gaps) sono esplicite',
      ddExample: 'Il supporto mancante diventa una domanda di follow-up (non un\'invenzione)',
      groundTruth: 'Lista open-items + Q&A col management (es. richiedi prova per add-back marketing)',
      variability: '3. Alternative accettate definite da esperti',
      variabilityRule: 'Esplicitare i gap documentali senza mai inventare rettifiche.',
      graderType: 'Model-based Judge + Expert Reference',
      verifierDesc: 'Confronta i gap segnalati con i riferimenti degli esperti',
      outputResult: 'PASS / FAIL + domande mancate',
      onlineScope: 'Segnala affermazioni prive di supporto e lacune visibili',
      outOfScopeOnline: 'Confermare che tutte le lacune e le domande necessarie siano state identificate'
    }
  };

  const themeBg = darkMode ? 'bg-[#14171A] text-[#ECEAE4]' : 'bg-[#F5F4EF] text-[#1C2024]';
  const cardBg = darkMode ? 'bg-[#1E2328] border-[#2E353B]' : 'bg-white border-[#E2E0D8]';
  const oliveCard = 'bg-[#788070] text-white';
  const oliveSoft = darkMode ? 'bg-[#2A302B] border-[#4A5448] text-[#D5DDD0]' : 'bg-[#E9EBE6] border-[#C8CEC2] text-[#2F352B]';

  return (
    <div className={`min-h-screen font-sans transition-colors duration-300 ${themeBg}`}>
      {/* HEADER PRINCIPALE */}
      <header className={`sticky top-0 z-30 border-b backdrop-blur-md ${darkMode ? 'bg-[#14171A]/90 border-[#2E353B]' : 'bg-[#F5F4EF]/90 border-[#DCD9CE]'}`}>
        <div className="max-w-7xl mx-auto px-4 sm:px-6 py-4 flex flex-col md:flex-row md:items-center md:justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-lg bg-[#788070] flex items-center justify-center text-white font-bold shadow-sm">
              <Scale className="w-6 h-6" />
            </div>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-xs font-bold uppercase tracking-widest px-2 py-0.5 rounded bg-[#788070]/20 text-[#788070]">
                  Scale AI • LA Tech Week
                </span>
                <span className="text-xs opacity-60">24 Slide Interattive</span>
              </div>
              <h1 className="text-lg sm:text-xl font-bold tracking-tight">
                Reliable Agents Need Evals: Caso Financial Due Diligence (QoE)
              </h1>
            </div>
          </div>

          {/* NAVIGAZIONE A 4 MODULI + TEMA */}
          <div className="flex flex-wrap items-center gap-1.5">
            {[
              { id: 'workflow', label: '1. Workflow & ROI', slides: 'Slide 1–3', icon: TrendingUp },
              { id: 'criteria', label: '2. I 5 Criteri & Grader', slides: 'Slide 4–10, 17', icon: CheckCircle2 },
              { id: 'benchmark', label: '3. Benchmark & Statistica', slides: 'Slide 11–18', icon: BarChart3 },
              { id: 'production', label: '4. Produzione & Dashboard', slides: 'Slide 19–24', icon: Activity }
            ].map((tab) => {
              const Icon = tab.icon;
              const isActive = activeTab === tab.id;
              return (
                <button
                  key={tab.id}
                  onClick={() => setActiveTab(tab.id)}
                  className={`px-3 py-2 rounded-lg text-xs sm:text-sm font-medium transition-all flex items-center gap-2 border ${
                    isActive
                      ? 'bg-[#788070] text-white border-[#788070] shadow-sm'
                      : darkMode
                      ? 'bg-[#1E2328] text-gray-300 border-[#2E353B] hover:bg-[#282E34]'
                      : 'bg-white text-gray-700 border-[#DCD9CE] hover:bg-gray-50'
                  }`}
                >
                  <Icon className="w-4 h-4 shrink-0" />
                  <div className="text-left">
                    <div>{tab.label}</div>
                    <div className={`text-[10px] leading-none ${isActive ? 'text-white/80' : 'opacity-50'}`}>
                      {tab.slides}
                    </div>
                  </div>
                </button>
              );
            })}

            <button
              onClick={() => setDarkMode(!darkMode)}
              className={`p-2.5 rounded-lg border ml-1 transition-colors ${
                darkMode ? 'bg-[#1E2328] border-[#2E353B] text-amber-400' : 'bg-white border-[#DCD9CE] text-slate-700'
              }`}
              title="Cambia tema Chiaro/Scuro"
            >
              <Sparkles className="w-4 h-4" />
            </button>
          </div>
        </div>
      </header>

      {/* CONTENUTO PRINCIPALE */}
      <main className="max-w-7xl mx-auto px-4 sm:px-6 py-8 space-y-8">
        
        {/* =====================================================================
            TAB 1: WORKFLOW FDD, SPECIFICA DEL TASK & ROI SIMULATOR (Slide 1-3)
           ===================================================================== */}
        {activeTab === 'workflow' && (
          <div className="space-y-8 animate-fadeIn">
            {/* Banner Introduttivo */}
            <div className={`p-6 rounded-2xl border shadow-sm ${oliveSoft}`}>
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider opacity-75">
                    Fase 1 • Definizione del Problema (Slide 1–3)
                  </span>
                  <h2 className="text-2xl font-bold mt-1">
                    Perché valutare un Agente di Financial Due Diligence (FDD)?
                  </h2>
                  <p className="text-sm opacity-90 mt-1 max-w-3xl">
                    L'obiettivo non è sostituire l'analista finanziario, ma <strong>renderlo più veloce</strong> (da 20 a 8 ore per pacchetto QoE). L'agente esplora la Virtual Data Room (VDR) e prepara la bozza; l'analista umano controlla le fonti, risolve i gap e approva il report finale.
                  </p>
                </div>
                <div className="shrink-0 bg-[#788070] text-white px-4 py-3 rounded-xl text-center">
                  <div className="text-xs uppercase tracking-wider opacity-80">Target Primario</div>
                  <div className="text-2xl font-extrabold">20h → 8h</div>
                  <div className="text-[11px] opacity-90">Ore analista / pacchetto QoE</div>
                </div>
              </div>
            </div>

            {/* SCHEMA ANIMATO DEL WORKFLOW FDD (Slide 3) */}
            <div className={`p-6 rounded-2xl border shadow-sm ${cardBg}`}>
              <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 mb-6">
                <div>
                  <h3 className="text-lg font-bold flex items-center gap-2">
                    <GitBranch className="w-5 h-5 text-[#788070]" />
                    Flusso Operativo FDD: Draft, Review & Approval (Slide 3)
                  </h3>
                  <p className="text-xs opacity-70">
                    Ambito: <strong>Una singola azienda / un periodo definito</strong> • Strumenti dell'Agente: <code>search</code>, <code>spreadsheet reader</code>, <code>terminal</code>
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => setIsLoopingEvidence(!isLoopingEvidence)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all flex items-center gap-1.5 ${
                      isLoopingEvidence
                        ? 'bg-amber-500 text-white border-amber-600'
                        : 'border-amber-500/40 text-amber-600 dark:text-amber-400 hover:bg-amber-500/10'
                    }`}
                  >
                    <HelpCircle className="w-3.5 h-3.5" />
                    Simula Evidenza Mancante
                  </button>
                  <button
                    onClick={() => setIsLoopingRevision(!isLoopingRevision)}
                    className={`px-3 py-1.5 rounded-lg text-xs font-semibold border transition-all flex items-center gap-1.5 ${
                      isLoopingRevision
                        ? 'bg-blue-600 text-white border-blue-700'
                        : 'border-blue-500/40 text-blue-600 dark:text-blue-400 hover:bg-blue-500/10'
                    }`}
                  >
                    <RefreshCw className="w-3.5 h-3.5" />
                    Simula Revisione Analista
                  </button>
                </div>
              </div>

              {/* Diagramma Visuale a 4 Blocchi */}
              <div className="grid grid-cols-1 md:grid-cols-4 gap-4 relative">
                {/* Blocco 0: INPUTS */}
                <div
                  onClick={() => setWorkflowStep(0)}
                  className={`cursor-pointer p-5 rounded-xl border-2 transition-all duration-300 ${
                    workflowStep === 0
                      ? 'border-[#788070] ring-4 ring-[#788070]/15 scale-[1.01]'
                      : 'border-transparent bg-black/5 dark:bg-white/5 opacity-85'
                  }`}
                >
                  <div className="text-[11px] font-bold uppercase tracking-wider opacity-60 mb-2">
                    INPUTS / STARTING STATE
                  </div>
                  <div className="flex items-center gap-2 font-bold text-base mb-3">
                    <Database className="w-5 h-5 text-[#788070]" />
                    Task Inputs
                  </div>
                  <ul className="text-xs space-y-2 opacity-85">
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#788070]" />
                      <strong>VDR snapshot</strong> ( mastri, schede, doc)
                    </li>
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#788070]" />
                      <strong>Scope + period</strong> (perimetro deal)
                    </li>
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#788070]" />
                      <strong>Draft template</strong> (modello vuoto)
                    </li>
                  </ul>
                </div>

                {/* Blocco 1: AGENT ANALYZE */}
                <div
                  onClick={() => setWorkflowStep(1)}
                  className={`cursor-pointer p-5 rounded-xl border-2 transition-all duration-300 ${oliveCard} ${
                    workflowStep === 1 ? 'ring-4 ring-[#788070]/40 scale-[1.02] shadow-lg' : 'opacity-90'
                  }`}
                >
                  <div className="flex items-center justify-between text-[11px] font-bold uppercase tracking-wider text-white/80 mb-2">
                    <span>AGENT / FASE 1</span>
                    <span className="px-2 py-0.5 rounded bg-white/20 text-white">Step 1</span>
                  </div>
                  <div className="flex items-center gap-2 font-bold text-base mb-3">
                    <Search className="w-5 h-5" />
                    Analyze Evidence
                  </div>
                  <ul className="text-xs space-y-2 text-white/90">
                    <li className="flex items-center gap-2">
                      <CheckCircle2 className="w-3.5 h-3.5 shrink-0" />
                      Search + read nella VDR
                    </li>
                    <li className="flex items-center gap-2">
                      <CheckCircle2 className="w-3.5 h-3.5 shrink-0" />
                      Riconciliazione delle cifre
                    </li>
                    <li className="flex items-center gap-2">
                      <CheckCircle2 className="w-3.5 h-3.5 shrink-0" />
                      Valutazione degli add-back
                    </li>
                  </ul>
                </div>

                {/* Blocco 2: AGENT PREPARE DRAFT */}
                <div
                  onClick={() => setWorkflowStep(2)}
                  className={`cursor-pointer p-5 rounded-xl border-2 transition-all duration-300 ${
                    workflowStep === 2
                      ? 'border-[#788070] ring-4 ring-[#788070]/15 scale-[1.01]'
                      : 'border-transparent bg-black/5 dark:bg-white/5 opacity-85'
                  }`}
                >
                  <div className="flex items-center justify-between text-[11px] font-bold uppercase tracking-wider opacity-60 mb-2">
                    <span>AGENT / FASE 2</span>
                    <span className="px-2 py-0.5 rounded bg-[#788070]/20 text-[#788070]">Step 2</span>
                  </div>
                  <div className="flex items-center gap-2 font-bold text-base mb-3">
                    <FileSpreadsheet className="w-5 h-5 text-[#788070]" />
                    Prepare Draft
                  </div>
                  <ul className="text-xs space-y-2 opacity-85">
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#788070]" />
                      <strong>QoE memo</strong> strutturato
                    </li>
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#788070]" />
                      <strong>EBITDA bridge</strong> (reported → adj)
                    </li>
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#788070]" />
                      <strong>Risks + questions</strong> (gap espliciti)
                    </li>
                  </ul>
                </div>

                {/* Blocco 3: ANALYST REVIEW & APPROVE */}
                <div
                  onClick={() => setWorkflowStep(3)}
                  className={`cursor-pointer p-5 rounded-xl border-2 transition-all duration-300 ${
                    workflowStep === 3
                      ? 'border-emerald-600 ring-4 ring-emerald-500/15 scale-[1.01]'
                      : 'border-transparent bg-black/5 dark:bg-white/5 opacity-85'
                  }`}
                >
                  <div className="flex items-center justify-between text-[11px] font-bold uppercase tracking-wider text-emerald-600 dark:text-emerald-400 mb-2">
                    <span>ANALYST / HUMAN</span>
                    <span className="px-2 py-0.5 rounded bg-emerald-500/20">Step 3</span>
                  </div>
                  <div className="flex items-center gap-2 font-bold text-base mb-3">
                    <Users className="w-5 h-5 text-emerald-600" />
                    Review + Approve
                  </div>
                  <ul className="text-xs space-y-2 opacity-85">
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-600" />
                      Controlla le fonti citate
                    </li>
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-600" />
                      Risolve i gap di evidenza
                    </li>
                    <li className="flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-emerald-600" />
                      <strong>Approva il report finale</strong>
                    </li>
                  </ul>
                </div>
              </div>

              {/* Loop Dinamici di Eccezione / Feedback (Slide 2 & 3) */}
              <div className="mt-4 grid grid-cols-1 md:grid-cols-2 gap-4">
                <div
                  className={`p-4 rounded-xl border transition-all ${
                    isLoopingEvidence
                      ? 'bg-amber-500/15 border-amber-500 ring-2 ring-amber-500/30'
                      : 'bg-amber-500/5 border-amber-500/20'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold uppercase tracking-wider text-amber-600 dark:text-amber-400 flex items-center gap-1.5">
                      <AlertTriangle className="w-4 h-4" />
                      Regola di Confine (Slide 2 & 3): Missing Support?
                    </span>
                    <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-amber-500/20 text-amber-700 dark:text-amber-300">
                      Step 1 → Follow-up Question
                    </span>
                  </div>
                  <p className="text-xs mt-1.5 opacity-85">
                    Quando le prove nella VDR sono insufficienti: <strong>segnala il gap e richiedi evidenza</strong>. Confine rigido per l'agente: <em className="underline font-semibold">Do not invent an adjustment</em> (Non inventare mai una rettifica).
                  </p>
                </div>

                <div
                  className={`p-4 rounded-xl border transition-all ${
                    isLoopingRevision
                      ? 'bg-blue-500/15 border-blue-500 ring-2 ring-blue-500/30'
                      : 'bg-blue-500/5 border-blue-500/20'
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-bold uppercase tracking-wider text-blue-600 dark:text-blue-400 flex items-center gap-1.5">
                      <RefreshCw className="w-4 h-4" />
                      Loop di Rilavorazione: Revisions / New Evidence
                    </span>
                    <span className="text-[11px] font-mono px-2 py-0.5 rounded bg-blue-500/20 text-blue-700 dark:text-blue-300">
                      Step 3 → Step 2
                    </span>
                  </div>
                  <p className="text-xs mt-1.5 opacity-85">
                    L'analista in Fase 3 può inviare correzioni o nuove prove raccolte dal management verso la Fase 2 per rigenerare la bozza prima dell'approvazione finale.
                  </p>
                </div>
              </div>
            </div>

            {/* SIMULATORE INTERATTIVO DI ROI & NET BENEFIT (Slide 1 - Worksheet 1) */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              {/* Colonna Sinistra: Sliders e Formula */}
              <div className={`lg:col-span-7 p-6 rounded-2xl border shadow-sm ${cardBg}`}>
                <div className="flex items-center justify-between mb-4">
                  <h3 className="text-lg font-bold flex items-center gap-2">
                    <Sliders className="w-5 h-5 text-[#788070]" />
                    Simulatore Business Outcome & Net Benefit (Slide 1)
                  </h3>
                  <span className="text-xs font-mono px-2 py-1 rounded bg-black/5 dark:bg-white/5">
                    Worksheet 1
                  </span>
                </div>

                <div className="space-y-5">
                  {/* Slider 1: Volume mensile */}
                  <div>
                    <div className="flex justify-between text-sm font-medium mb-1">
                      <span>Volume atteso (Pacchetti QoE / mese)</span>
                      <span className="font-bold text-[#788070]">{packagesPerMonth} pacchetti/mese</span>
                    </div>
                    <input
                      type="range"
                      min="5"
                      max="60"
                      step="5"
                      value={packagesPerMonth}
                      onChange={(e) => setPackagesPerMonth(Number(e.target.value))}
                      className="w-full accent-[#788070] cursor-pointer"
                    />
                    <div className="flex justify-between text-[11px] opacity-60">
                      <span>5 deal/mese</span>
                      <span>Default Slide: 20 deal/mese</span>
                      <span>60 deal/mese</span>
                    </div>
                  </div>

                  {/* Slider 2: Baseline Hours */}
                  <div>
                    <div className="flex justify-between text-sm font-medium mb-1">
                      <span>Baseline attuale (Ore analista senza Agente)</span>
                      <span className="font-bold">{baselineHours} ore / pacchetto</span>
                    </div>
                    <input
                      type="range"
                      min="10"
                      max="40"
                      step="1"
                      value={baselineHours}
                      onChange={(e) => setBaselineHours(Number(e.target.value))}
                      className="w-full accent-[#788070] cursor-pointer"
                    />
                  </div>

                  {/* Slider 3: Target Hours */}
                  <div>
                    <div className="flex justify-between text-sm font-medium mb-1">
                      <span>Target con Agente (Include review e correzione standard)</span>
                      <span className="font-bold text-emerald-600">{targetHours} ore / pacchetto</span>
                    </div>
                    <input
                      type="range"
                      min="4"
                      max="18"
                      step="1"
                      value={targetHours}
                      onChange={(e) => setTargetHours(Number(e.target.value))}
                      className="w-full accent-emerald-600 cursor-pointer"
                    />
                    <div className="flex justify-between text-[11px] opacity-60">
                      <span>4h</span>
                      <span>Target Slide: 8 ore</span>
                      <span>18h</span>
                    </div>
                  </div>

                  {/* Slider 4: Costi di Rework / Errori non intercettati */}
                  <div>
                    <div className="flex justify-between text-sm font-medium mb-1">
                      <span>Rilavorazione extra per errori/allucinazioni (Rework & Setup)</span>
                      <span className="font-bold text-amber-600">+{reworkOverhead} ore / pacchetto</span>
                    </div>
                    <input
                      type="range"
                      min="0"
                      max="8"
                      step="0.5"
                      value={reworkOverhead}
                      onChange={(e) => setReworkOverhead(Number(e.target.value))}
                      className="w-full accent-amber-600 cursor-pointer"
                    />
                    <p className="text-[11px] opacity-70 mt-1">
                      Nota dalla Slide 1: <em>"Net value = labor value saved minus incremental costs. Avoid double-counting review and correction."</em>
                    </p>
                  </div>
                </div>
              </div>

              {/* Colonna Destra: Risultati Visuali e Tabella Slide 1 & 2 */}
              <div className={`lg:col-span-5 p-6 rounded-2xl border shadow-sm flex flex-col justify-between ${cardBg}`}>
                <div>
                  <h4 className="text-sm font-bold uppercase tracking-wider opacity-70 mb-4">
                    Impatto Mensile Stimato
                  </h4>

                  <div className="grid grid-cols-2 gap-4 mb-6">
                    <div className="p-4 rounded-xl bg-[#788070]/15 border border-[#788070]/30">
                      <div className="text-xs opacity-75">Risparmio Lordo Pianificato</div>
                      <div className="text-3xl font-extrabold text-[#788070] mt-1">
                        {totalMonthlyHoursSaved}h
                      </div>
                      <div className="text-[11px] opacity-75 mt-1">
                        ({grossHoursSavedPerPkg}h risparmiate × {packagesPerMonth} deal)
                      </div>
                    </div>

                    <div className="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/30">
                      <div className="text-xs opacity-75">Beneficio Netto Effettivo</div>
                      <div className="text-3xl font-extrabold text-emerald-600 dark:text-emerald-400 mt-1">
                        {totalNetMonthlyHours}h
                      </div>
                      <div className="text-[11px] opacity-75 mt-1">
                        Al netto di +{reworkOverhead}h rework/deal
                      </div>
                    </div>
                  </div>

                  {/* Barra comparativa tempi */}
                  <div className="space-y-3 text-xs">
                    <div>
                      <div className="flex justify-between mb-1">
                        <span>Oggi (Baseline manuale)</span>
                        <span className="font-bold">{baselineHours}h / deal</span>
                      </div>
                      <div className="w-full h-3 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">
                        <div className="h-full bg-slate-500" style={{ width: '100%' }} />
                      </div>
                    </div>

                    <div>
                      <div className="flex justify-between mb-1">
                        <span>Con Agente FDD (Target + Rework)</span>
                        <span className="font-bold">
                          {targetHours}h + {reworkOverhead}h = {targetHours + reworkOverhead}h / deal
                        </span>
                      </div>
                      <div className="w-full h-3 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden flex">
                        <div
                          className="h-full bg-emerald-600 transition-all"
                          style={{ width: `${Math.min(100, (targetHours / baselineHours) * 100)}%` }}
                          title="Target Hours"
                        />
                        <div
                          className="h-full bg-amber-500 transition-all"
                          style={{ width: `${Math.min(100, (reworkOverhead / baselineHours) * 100)}%` }}
                          title="Rework Hours"
                        />
                      </div>
                    </div>
                  </div>
                </div>

                <div className="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700/60 text-xs space-y-1.5 opacity-85">
                  <div className="font-bold text-[#788070]">Regola di Misurazione (Slide 1):</div>
                  <p>
                    • Misurare sempre <strong>lo stesso perimetro e la stessa qualità</strong> (tracciando errori materiali e omissioni).
                  </p>
                  <p>
                    • Includere nei costi: costo di esecuzione dell'agente, tempo di revisione analista, rilavorazione (rework) e setup iniziale.
                  </p>
                </div>
              </div>
            </div>
          </div>
        )}

        {/* =====================================================================
            TAB 2: I 5 CRITERI, GATE SIMULATOR & CODICE GRADER (Slide 4-10, 17)
           ===================================================================== */}
        {activeTab === 'criteria' && (
          <div className="space-y-8 animate-fadeIn">
            {/* SIMULATORE DEL GATE "ALL 5 MUST PASS" (Slide 4, 10, 17) */}
            <div className={`p-6 rounded-2xl border shadow-sm ${cardBg}`}>
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                    Slide 4, 10 & 17 • Regola di Scoring Rigorosa
                  </span>
                  <h2 className="text-xl font-bold mt-1">
                    I 5 Criteri Osservabili di Correttezza (Gate "All-5-Must-Pass")
                  </h2>
                  <p className="text-xs opacity-75 mt-1">
                    Clicca su ciascun criterio per ispezionare Ground Truth, Variabilità e Grader, oppure attiva/disattiva l'interruttore <strong>PASS / FAIL</strong> per vedere l'impatto sullo score finale della bozza QoE.
                  </p>
                </div>

                {/* Badge Risultato Globale Task */}
                <div
                  className={`px-5 py-3 rounded-xl border-2 flex items-center gap-3 shrink-0 transition-all ${
                    allFivePass
                      ? 'bg-emerald-500/15 border-emerald-500 text-emerald-700 dark:text-emerald-300'
                      : 'bg-rose-500/15 border-rose-500 text-rose-700 dark:text-rose-300'
                  }`}
                >
                  {allFivePass ? (
                    <CheckCircle2 className="w-8 h-8 text-emerald-600 shrink-0" />
                  ) : (
                    <XCircle className="w-8 h-8 text-rose-600 shrink-0" />
                  )}
                  <div>
                    <div className="text-[11px] font-bold uppercase tracking-wider">
                      Task Score: {allFivePass ? '1 (PASS)' : '0 (FAIL)'}
                    </div>
                    <div className="text-sm font-extrabold">
                      {allFivePass
                        ? 'Tutti i 5 criteri superati!'
                        : 'Bozza Bocciata (Almeno 1 criterio FAIL)'}
                    </div>
                  </div>
                </div>
              </div>

              {/* Griglia dei 5 Criteri C1-C5 */}
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-5 gap-3">
                {Object.values(criteriaData).map((crit) => {
                  const isPassing = criteriaState[crit.id];
                  const isSelected = selectedCriterion === crit.id;
                  return (
                    <div
                      key={crit.id}
                      onClick={() => setSelectedCriterion(crit.id)}
                      className={`p-4 rounded-xl border-2 cursor-pointer transition-all flex flex-col justify-between ${
                        isSelected
                          ? 'border-[#788070] ring-4 ring-[#788070]/15 bg-[#788070]/5'
                          : 'border-gray-200 dark:border-gray-700/70 hover:border-[#788070]/50'
                      }`}
                    >
                      <div>
                        <div className="flex items-center justify-between gap-2 mb-2">
                          <span className="text-xs font-extrabold px-2 py-0.5 rounded bg-[#788070] text-white">
                            {crit.id}
                          </span>
                          <button
                            onClick={(e) => {
                              e.stopPropagation();
                              setCriteriaState((prev) => ({
                                ...prev,
                                [crit.id]: !prev[crit.id]
                              }));
                            }}
                            className={`px-2.5 py-0.5 rounded-full text-[11px] font-bold transition-colors ${
                              isPassing
                                ? 'bg-emerald-600 text-white'
                                : 'bg-rose-600 text-white'
                            }`}
                          >
                            {isPassing ? 'PASS ✓' : 'FAIL ✗'}
                          </button>
                        </div>
                        <div className="font-bold text-sm">{crit.name}</div>
                        <div className="text-xs opacity-70 mt-0.5">{crit.titleIt}</div>
                      </div>

                      <div className="mt-4 pt-3 border-t border-gray-200 dark:border-gray-700/50 text-[11px] opacity-80">
                        {crit.whatMustBeTrue}
                      </div>
                    </div>
                  );
                })}
              </div>

              {/* Caso Studio Slide 17 Warning Box */}
              {!criteriaState.C2 && criteriaState.C1 && (
                <div className="mt-4 p-4 rounded-xl bg-rose-500/10 border border-rose-500/30 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
                  <div className="flex items-start gap-3">
                    <ShieldAlert className="w-5 h-5 text-rose-600 shrink-0 mt-0.5" />
                    <div className="text-xs">
                      <span className="font-bold text-rose-600 dark:text-rose-400">
                        Esempio esatto della Slide 17 ("Task success on the final QoE draft"):
                      </span>{' '}
                      Anche se <strong>C1 PASSA</strong> (le cifre fonte sono corrette e l'EBITDA bridge quadra matematicamente), se <strong>C2 FALLISCE</strong> perché la bozza omette un rischio materiale presente nel riferimento dell'esperto, il risultato complessivo è <strong>OVERALL FAIL (Score = 0)</strong>.
                    </div>
                  </div>
                  <button
                    onClick={() => setCriteriaState({ C1: true, C2: true, C3: true, C4: true, C5: true })}
                    className="px-3 py-1.5 rounded-lg bg-emerald-600 text-white text-xs font-bold shrink-0 hover:bg-emerald-700"
                  >
                    Ripristina tutti a PASS
                  </button>
                </div>
              )}

              {/* Dettaglio del Criterio Selezionato (Slide 4, 5, 6, 10) */}
              <div className={`mt-6 p-5 rounded-xl border ${oliveSoft}`}>
                <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-2 pb-4 border-b border-[#788070]/20">
                  <div>
                    <span className="text-xs font-bold uppercase tracking-wider opacity-75">
                      Radiografia Completa del Criterio Selezionato
                    </span>
                    <h3 className="text-lg font-bold">
                      {criteriaData[selectedCriterion].id}: {criteriaData[selectedCriterion].name} ({criteriaData[selectedCriterion].titleIt})
                    </h3>
                  </div>
                  <span className="text-xs font-semibold px-3 py-1 rounded-full bg-[#788070] text-white">
                    {criteriaData[selectedCriterion].graderType}
                  </span>
                </div>

                <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mt-4 text-xs">
                  <div className="p-3 rounded-lg bg-white/60 dark:bg-black/20">
                    <div className="font-bold uppercase opacity-60 mb-1">Slide 4 • Esempio FDD</div>
                    <div className="font-semibold text-sm">{criteriaData[selectedCriterion].ddExample}</div>
                    <div className="mt-1 opacity-80">{criteriaData[selectedCriterion].whatMustBeTrue}</div>
                  </div>

                  <div className="p-3 rounded-lg bg-white/60 dark:bg-black/20">
                    <div className="font-bold uppercase opacity-60 mb-1">Slide 5 • Ground Truth</div>
                    <div className="font-semibold">{criteriaData[selectedCriterion].groundTruth}</div>
                  </div>

                  <div className="p-3 rounded-lg bg-white/60 dark:bg-black/20">
                    <div className="font-bold uppercase opacity-60 mb-1">Slide 6 • Variabilità Ammessa</div>
                    <div className="font-semibold text-[#788070] dark:text-emerald-300">
                      {criteriaData[selectedCriterion].variability}
                    </div>
                    <div className="mt-1 opacity-80">{criteriaData[selectedCriterion].variabilityRule}</div>
                  </div>

                  <div className="p-3 rounded-lg bg-white/60 dark:bg-black/20">
                    <div className="font-bold uppercase opacity-60 mb-1">Slide 10 • Output del Grader</div>
                    <div className="font-mono font-bold text-xs">{criteriaData[selectedCriterion].outputResult}</div>
                    <div className="mt-1 opacity-80">{criteriaData[selectedCriterion].verifierDesc}</div>
                  </div>
                </div>
              </div>
            </div>

            {/* I 3 TIPI DI GRADER & ISPETTORE CODICE / PROMPT (Slide 7, 8, 9, 21) */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              {/* I 3 Pilastri dell'Hybrid Grading (Slide 7) */}
              <div className={`lg:col-span-5 p-6 rounded-2xl border shadow-sm space-y-4 ${cardBg}`}>
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                    Slide 7 • Hybrid Grading Architecture
                  </span>
                  <h3 className="text-lg font-bold mt-1">
                    Combinare Codice, Giudici LLM ed Esperti
                  </h3>
                </div>

                <div className="space-y-3">
                  <div className="p-4 rounded-xl bg-[#788070] text-white">
                    <div className="flex items-center justify-between text-xs font-bold uppercase tracking-wider text-white/80">
                      <span>1. Deterministic Checks</span>
                      <span>REPRODUCIBLE</span>
                    </div>
                    <p className="text-xs mt-1.5 text-white/95">
                      Controlli oggettivi via codice su valori, schemi, test e stato finale. Accetta output alternativi validi entro tolleranze numeriche (usato per <strong>C1</strong>).
                    </p>
                  </div>

                  <div className="p-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-black/5 dark:bg-white/5">
                    <div className="flex items-center justify-between text-xs font-bold uppercase tracking-wider text-[#788070]">
                      <span>2. Model-based Judges</span>
                      <span>FLEXIBLE</span>
                    </div>
                    <p className="text-xs mt-1.5 opacity-85">
                      Rubriche esplicite per criteri aperti. Usa decisioni binarie (<code>PASS</code> / <code>FAIL</code> / <code>NEEDS_REVIEW</code>) o scale ancorate, poi <strong>calibra sugli esperti</strong> (usato per <strong>C2–C5</strong>).
                    </p>
                  </div>

                  <div className="p-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-black/5 dark:bg-white/5">
                    <div className="flex items-center justify-between text-xs font-bold uppercase tracking-wider text-amber-600">
                      <span>3. Human Evaluation</span>
                      <span>EXPERT REVIEW</span>
                    </div>
                    <p className="text-xs mt-1.5 opacity-85">
                      Etichette degli esperti FDD, calibrazione dei giudici, audit a campione e risoluzione dei casi disputati (<code>NEEDS_REVIEW</code>). Costo più alto → throughput limitato.
                    </p>
                  </div>
                </div>
              </div>

              {/* Live Inspector: Codice Python C1 (Slide 8) + Prompt C2 (Slide 9) + Prompt C3 Online (Slide 21) */}
              <div className={`lg:col-span-7 p-6 rounded-2xl border shadow-sm flex flex-col justify-between ${cardBg}`}>
                <div>
                  <div className="flex flex-wrap items-center justify-between gap-2 mb-4">
                    <h3 className="text-base font-bold flex items-center gap-2">
                      <Code2 className="w-5 h-5 text-[#788070]" />
                      Laboratorio Live: Verificatori e Prompt Reali delle Slide
                    </h3>
                    <div className="flex gap-1">
                      {[
                        { id: 'c1_code', label: 'Slide 8: C1 Python Verifier' },
                        { id: 'c2_prompt', label: 'Slide 9: C2 Judge (Offline)' },
                        { id: 'c3_prompt', label: 'Slide 21: C3 Judge (Online)' }
                      ].map((t) => (
                        <button
                          key={t.id}
                          onClick={() => setInspectorTab(t.id)}
                          className={`px-2.5 py-1 rounded text-xs font-semibold transition-all ${
                            inspectorTab === t.id
                              ? 'bg-[#788070] text-white'
                              : 'bg-black/5 dark:bg-white/10 opacity-75 hover:opacity-100'
                          }`}
                        >
                          {t.label}
                        </button>
                      ))}
                    </div>
                  </div>

                  {/* Contenuto Tab 1: C1 Python Verifier (Slide 8) */}
                  {inspectorTab === 'c1_code' && (
                    <div className="space-y-4">
                      <pre className="p-4 rounded-xl bg-[#1E2328] text-gray-100 text-xs font-mono overflow-x-auto leading-relaxed">
{`def verify_bridge(draft, reference, tol):
    keys = ["reported_ebitda", "adjustment_total", "adjusted_ebitda"]
    if any(k not in draft for k in keys):
        return {"pass": False, "reason": "missing value"}
    checks = {k: abs(draft[k] - reference[k]) <= tol for k in keys}
    checks["reconciles"] = abs(
        draft["reported_ebitda"] + draft["adjustment_total"]
        - draft["adjusted_ebitda"]
    ) <= tol
    return {"pass": all(checks.values()), "checks": checks}`}
                      </pre>

                      {/* Simulatore Live della funzione Python */}
                      <div className="p-4 rounded-xl border border-gray-200 dark:border-gray-700 bg-black/5 dark:bg-white/5">
                        <div className="flex items-center justify-between mb-3">
                          <span className="text-xs font-bold uppercase tracking-wider">
                            Testa la funzione <code>verify_bridge()</code> in tempo reale (Rif: 10.0 + 5.2 = 15.2 $M)
                          </span>
                          <span
                            className={`px-2.5 py-0.5 rounded text-xs font-mono font-bold ${
                              c1AllPass ? 'bg-emerald-600 text-white' : 'bg-rose-600 text-white'
                            }`}
                          >
                            pass: {String(c1AllPass)}
                          </span>
                        </div>

                        <div className="grid grid-cols-3 gap-3 text-xs">
                          <div>
                            <label className="block opacity-70 mb-1">draft["reported_ebitda"]</label>
                            <input
                              type="number"
                              step="0.1"
                              value={draftReported}
                              onChange={(e) => setDraftReported(Number(e.target.value))}
                              className="w-full px-2.5 py-1.5 rounded border bg-white dark:bg-[#14171A] font-mono"
                            />
                            <div className={`mt-1 font-mono text-[11px] ${c1Checks.reported_ebitda ? 'text-emerald-600' : 'text-rose-600'}`}>
                              check: {String(c1Checks.reported_ebitda)}
                            </div>
                          </div>

                          <div>
                            <label className="block opacity-70 mb-1">draft["adjustment_total"]</label>
                            <input
                              type="number"
                              step="0.1"
                              value={draftAdj}
                              onChange={(e) => setDraftAdj(Number(e.target.value))}
                              className="w-full px-2.5 py-1.5 rounded border bg-white dark:bg-[#14171A] font-mono"
                            />
                            <div className={`mt-1 font-mono text-[11px] ${c1Checks.adjustment_total ? 'text-emerald-600' : 'text-rose-600'}`}>
                              check: {String(c1Checks.adjustment_total)}
                            </div>
                          </div>

                          <div>
                            <label className="block opacity-70 mb-1">draft["adjusted_ebitda"]</label>
                            <input
                              type="number"
                              step="0.1"
                              value={draftFinal}
                              onChange={(e) => setDraftFinal(Number(e.target.value))}
                              className="w-full px-2.5 py-1.5 rounded border bg-white dark:bg-[#14171A] font-mono"
                            />
                            <div className={`mt-1 font-mono text-[11px] ${c1Checks.adjusted_ebitda ? 'text-emerald-600' : 'text-rose-600'}`}>
                              check: {String(c1Checks.adjusted_ebitda)} | reconciles: {String(c1Checks.reconciles)}
                            </div>
                          </div>
                        </div>
                        <p className="text-[11px] opacity-75 mt-3">
                          Nota cruciale dalla Slide 8: <em>"Correct arithmetic does not justify an add-back. C4 checks the analytical treatment."</em>
                        </p>
                      </div>
                    </div>
                  )}

                  {/* Contenuto Tab 2: C2 Judge Prompt (Slide 9) */}
                  {inspectorTab === 'c2_prompt' && (
                    <div className="space-y-3">
                      <pre className="p-4 rounded-xl bg-[#1E2328] text-gray-100 text-xs font-mono whitespace-pre-wrap leading-relaxed">
{`Evaluate the QoE memo against the reference.

TASK: {engagement_scope_and_period}
DRAFT: {qoe_memo_and_bridge}
GROUND TRUTH: {material_findings_and_evidence}

Check that every required finding is included.
Accept equivalent wording with the same meaning.
Return PASS or FAIL, with each missing finding
and its reference evidence. If the reference
is insufficient, return NEEDS_REVIEW.`}
                      </pre>
                      <div className="p-3.5 rounded-xl bg-amber-500/10 border border-amber-500/30 text-xs space-y-1">
                        <div className="font-bold text-amber-600 dark:text-amber-400">
                          Perché restituire NEEDS_REVIEW? (Slide 9 & 10)
                        </div>
                        <p className="opacity-85">
                          <code>NEEDS_REVIEW</code> non viene mai contato come <code>PASS</code>. Indica un difetto nel riferimento o un'ambiguità che deve essere risolta da un esperto prima di fidarsi dello score del benchmark.
                        </p>
                      </div>
                    </div>
                  )}

                  {/* Contenuto Tab 3: C3 Online Judge Prompt (Slide 21) */}
                  {inspectorTab === 'c3_prompt' && (
                    <div className="space-y-3">
                      <pre className="p-4 rounded-xl bg-[#1E2328] text-gray-100 text-xs font-mono whitespace-pre-wrap leading-relaxed">
{`Evaluate support for each material QoE claim.

TASK: {scope_and_period}
DRAFT: {qoe_memo_bridge_and_citations}
SOURCES: {vdr_documents_and_spreadsheets}

Cite a document passage or cell for each claim.
Return FAIL for unsupported or contradicted claims.
Return NEEDS_REVIEW for missing or ambiguous evidence.
Otherwise return PASS with supporting evidence.
No reference QoE analysis is supplied.`}
                      </pre>
                      <div className="p-3.5 rounded-xl bg-blue-500/10 border border-blue-500/30 text-xs space-y-1">
                        <div className="font-bold text-blue-600 dark:text-blue-400">
                          Differenza chiave in Produzione (Slide 21):
                        </div>
                        <p className="opacity-85">
                          Nota l'ultima riga: <code>No reference QoE analysis is supplied.</code> In produzione non abbiamo il report scritto dall'esperto, ma abbiamo i file della VDR! Il giudice C3 verifica ogni citazione direttamente sui documenti/fogli di calcolo.
                        </p>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* =====================================================================
            TAB 3: BENCHMARK, "GRADER TRAP" & STATISTICA (Slide 11-16, 18)
           ===================================================================== */}
        {activeTab === 'benchmark' && (
          <div className="space-y-8 animate-fadeIn">
            {/* SIMULATORE "THE GRADER TRAP" - VALIDAZIONE CONTRO ETICHETTE ESPERTI (Slide 15 & 16) */}
            <div className={`p-6 rounded-2xl border shadow-sm ${cardBg}`}>
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-rose-600">
                    Slide 15 & 16 • Meta-Valutazione: Chi valuta il Valutatore?
                  </span>
                  <h2 className="text-xl font-bold mt-1">
                    La Trappola dell'Accordo al 90% ("Grader Validation against Expert Labels")
                  </h2>
                  <p className="text-xs opacity-75 mt-1 max-w-3xl">
                    Gli esperti FDD etichettano <strong>100 bozze</strong>: <strong>90 corrette (Good)</strong> e <strong>10 errate (Bad)</strong>. Guarda cosa succede se il tuo Grader promuove tutte e 100 le bozze senza bloccare alcun errore:
                  </p>
                </div>

                {/* Selettore Preset Slide 15 */}
                <div className="flex flex-wrap gap-1.5 shrink-0">
                  <button
                    onClick={() => applyGraderPreset('lenient')}
                    className={`px-3 py-1.5 rounded-lg text-xs font-bold border transition-all ${
                      graderPreset === 'lenient'
                        ? 'bg-rose-600 text-white border-rose-600'
                        : 'border-rose-500/40 text-rose-600 hover:bg-rose-500/10'
                    }`}
                  >
                    Caso Slide 15 (Passa tutte le 100)
                  </button>
                  <button
                    onClick={() => applyGraderPreset('strict')}
                    className={`px-3 py-1.5 rounded-lg text-xs font-bold border transition-all ${
                      graderPreset === 'strict'
                        ? 'bg-amber-600 text-white border-amber-600'
                        : 'border-amber-500/40 text-amber-600 hover:bg-amber-500/10'
                    }`}
                  >
                    Grader Troppo Severo
                  </button>
                  <button
                    onClick={() => applyGraderPreset('calibrated')}
                    className={`px-3 py-1.5 rounded-lg text-xs font-bold border transition-all ${
                      graderPreset === 'calibrated'
                        ? 'bg-emerald-600 text-white border-emerald-600'
                        : 'border-emerald-500/40 text-emerald-600 hover:bg-emerald-500/10'
                    }`}
                  >
                    Grader Calibrato
                  </button>
                </div>
              </div>

              {/* Le 3 Card giganti identiche alla Slide 15 */}
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6">
                <div className="p-6 rounded-2xl bg-[#788070] text-white">
                  <div className="text-xs font-bold uppercase tracking-wider text-white/80">
                    OVERALL AGREEMENT
                  </div>
                  <div className="text-5xl font-extrabold my-3">{overallAgreement}%</div>
                  <div className="text-xs text-white/90">
                    {overallAgreement} su 100 etichette coincidono con gli esperti
                  </div>
                </div>

                <div
                  className={`p-6 rounded-2xl border-2 transition-colors ${
                    badDraftsPassed >= 5
                      ? 'bg-rose-600 text-white border-rose-700'
                      : 'bg-[#788070]/90 text-white border-transparent'
                  }`}
                >
                  <div className="flex items-center justify-between text-xs font-bold uppercase tracking-wider text-white/90">
                    <span>BAD DRAFTS PASSED (Falsi Accetti)</span>
                    {badDraftsPassed === 10 && <AlertTriangle className="w-4 h-4" />}
                  </div>
                  <div className="text-5xl font-extrabold my-3">
                    {badDraftsPassed} / 10
                  </div>
                  <div className="text-xs text-white/95 font-medium">
                    {badDraftsPassed === 10
                      ? 'Every failure is missed! (100% errori critici passati)'
                      : `${badDraftsPassed * 10}% delle bozze errate sfugge al grader`}
                  </div>
                </div>

                <div className="p-6 rounded-2xl bg-[#788070]/80 text-white">
                  <div className="text-xs font-bold uppercase tracking-wider text-white/80">
                    GOOD DRAFTS FAILED (Falsi Rifiuti)
                  </div>
                  <div className="text-5xl font-extrabold my-3">
                    {goodDraftsFailed} / 90
                  </div>
                  <div className="text-xs text-white/90">
                    {goodDraftsFailed === 0
                      ? 'Nessuna bozza corretta viene rigettata'
                      : `${goodDraftsFailed} bozze buone mandate inutilmente in revisione`}
                  </div>
                </div>
              </div>

              {/* Sliders per sperimentare i due tipi di errore + Impatto Business (Slide 16) */}
              <div className="grid grid-cols-1 md:grid-cols-2 gap-6 pt-4 border-t border-gray-200 dark:border-gray-700">
                <div className="space-y-3">
                  <div className="flex justify-between text-xs font-bold">
                    <span className="text-rose-600 dark:text-rose-400">
                      Errore Tipo 1: Bozze Errate Promosse (su 10 Bad Drafts)
                    </span>
                    <span>{badDraftsPassed} / 10</span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="10"
                    value={badDraftsPassed}
                    onChange={(e) => {
                      setGraderPreset('custom');
                      setBadDraftsPassed(Number(e.target.value));
                    }}
                    className="w-full accent-rose-600 cursor-pointer"
                  />
                  <div className="p-3.5 rounded-xl bg-rose-500/10 border border-rose-500/20 text-xs">
                    <div className="font-bold text-rose-600 dark:text-rose-400">
                      Slide 16 • Impatto Business: Unsupported EBITDA add-back (HIGH COST)
                    </div>
                    <p className="opacity-85 mt-1">
                      Sovrastima l'EBITDA rettificato e può <strong>distorcere l'analisi del deal team</strong>. Richiede limiti estremamente severi.
                    </p>
                  </div>
                </div>

                <div className="space-y-3">
                  <div className="flex justify-between text-xs font-bold">
                    <span className="text-amber-600 dark:text-amber-400">
                      Errore Tipo 2: Bozze Buone Bocciate (su 90 Good Drafts)
                    </span>
                    <span>{goodDraftsFailed} / 90</span>
                  </div>
                  <input
                    type="range"
                    min="0"
                    max="30"
                    value={goodDraftsFailed}
                    onChange={(e) => {
                      setGraderPreset('custom');
                      setGoodDraftsFailed(Number(e.target.value));
                    }}
                    className="w-full accent-amber-600 cursor-pointer"
                  />
                  <div className="p-3.5 rounded-xl bg-amber-500/10 border border-amber-500/20 text-xs">
                    <div className="font-bold text-amber-600 dark:text-amber-400">
                      Slide 16 • Impatto Business: Extra Review vs Missed Risk (SET SEPARATE LIMITS)
                    </div>
                    <p className="opacity-85 mt-1">
                      Un rischio materiale mancato impatta il deal; un follow-up non necessario consuma solo tempo dell'analista. Fissa limiti separati per i due errori!
                    </p>
                  </div>
                </div>
              </div>
            </div>

            {/* COSTRUZIONE DEL BENCHMARK (400 TASK) & SIGNIFICATIVITÀ STATISTICA (Slide 11-14, 18) */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              {/* Costruzione e Copertura del Benchmark (Slide 11 & 14) */}
              <div className={`lg:col-span-6 p-6 rounded-2xl border shadow-sm space-y-5 ${cardBg}`}>
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                    Slide 11–14 • Benchmark Construction & Split
                  </span>
                  <h3 className="text-lg font-bold mt-1">
                    Copertura Stratificata su 400 Task & Prevenzione Data Leakage
                  </h3>
                </div>

                {/* Stratificazione 400 Task */}
                <div className="space-y-3 text-xs">
                  <div className="p-3.5 rounded-xl border border-gray-200 dark:border-gray-700">
                    <div className="flex justify-between font-bold mb-1.5">
                      <span>Complessità Entità: Single (70%) vs Multiple Entities (30%)</span>
                      <span className="text-[#788070]">280 / 120 task</span>
                    </div>
                    <div className="w-full h-3 rounded-full overflow-hidden flex">
                      <div className="bg-[#788070] h-full" style={{ width: '70%' }} />
                      <div className="bg-amber-500 h-full" style={{ width: '30%' }} />
                    </div>
                  </div>

                  <div className="p-3.5 rounded-xl border border-gray-200 dark:border-gray-700">
                    <div className="flex justify-between font-bold mb-1.5">
                      <span>Qualità Dati VDR: Reconciled (60%) vs Conflicting Data (40%)</span>
                      <span className="text-[#788070]">240 / 160 task</span>
                    </div>
                    <div className="w-full h-3 rounded-full overflow-hidden flex">
                      <div className="bg-emerald-600 h-full" style={{ width: '60%' }} />
                      <div className="bg-rose-500 h-full" style={{ width: '40%' }} />
                    </div>
                  </div>
                </div>

                {/* I 4 Livelli della Slide 14 */}
                <div className="space-y-2 text-xs">
                  <div className="font-bold uppercase opacity-70">
                    Slide 14 • Gestione Ciclo di Vita dei Task:
                  </div>
                  {[
                    {
                      tag: 'DEVELOPMENT',
                      title: 'Agent & grader tuning',
                      desc: 'Usa un pilota (20–50 task, Slide 12) per fare debug di task, riferimenti e difetti del grader.'
                    },
                    {
                      tag: 'REGRESSION',
                      title: 'Known behavior',
                      desc: 'Mantieni i fallimenti risolti e le tipologie di task precedentemente affidabili.'
                    },
                    {
                      tag: 'FINAL EVALUATION',
                      title: 'Untouched holdout',
                      desc: 'Dividi per deal o azienda target PRIMA del tuning; tieni uniti i periodi correlati dello stesso deal!'
                    },
                    {
                      tag: 'REPRODUCIBILITY',
                      title: 'Pinned configuration',
                      desc: 'Versiona task, VDR locale (Slide 13), riferimenti, grader, tool e resetta lo stato ad ogni run.'
                    }
                  ].map((item) => (
                    <div
                      key={item.tag}
                      className="p-3 rounded-lg bg-black/5 dark:bg-white/5 flex items-start gap-3"
                    >
                      <span className="text-[10px] font-extrabold px-2 py-1 rounded bg-[#788070] text-white shrink-0 w-32 text-center">
                        {item.tag}
                      </span>
                      <div>
                        <span className="font-bold">{item.title}: </span>
                        <span className="opacity-85">{item.desc}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Esploratore Dimensione del Campione e Significatività Statistica (Slide 18) */}
              <div className={`lg:col-span-6 p-6 rounded-2xl border shadow-sm flex flex-col justify-between ${cardBg}`}>
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                    Slide 18 • Statistical Significance & Sample Size
                  </span>
                  <h3 className="text-lg font-bold mt-1">
                    Perché 20 deal non bastano? Stabilità dello Score & Rischi Rari
                  </h3>
                  <p className="text-xs opacity-75 mt-1">
                    Trascina lo slider per vedere come cambia l'intervallo di confidenza al 95% (attorno a un pass rate del 95%) e il limite superiore di rischio quando osservi <strong>zero errori</strong>:
                  </p>

                  {/* Slider Sample Size */}
                  <div className="my-5 p-4 rounded-xl bg-black/5 dark:bg-white/5">
                    <div className="flex justify-between items-center mb-2">
                      <span className="text-xs font-bold uppercase">Dimensione Campione (Numero di Deal Indipendenti)</span>
                      <span className="text-lg font-extrabold text-[#788070]">{sampleSize} deal</span>
                    </div>
                    <input
                      type="range"
                      min="20"
                      max="400"
                      step="10"
                      value={sampleSize}
                      onChange={(e) => setSampleSize(Number(e.target.value))}
                      className="w-full accent-[#788070] cursor-pointer"
                    />
                    <div className="flex justify-between text-[11px] opacity-60 mt-1">
                      <button onClick={() => setSampleSize(30)} className="underline">Pilota (30 deal)</button>
                      <button onClick={() => setSampleSize(100)} className="underline">Slide: 100 deal (89–98%)</button>
                      <button onClick={() => setSampleSize(300)} className="underline">Slide: 300 deal (&lt;1% risk)</button>
                      <button onClick={() => setSampleSize(400)} className="underline">Benchmark: 400 deal (92–97%)</button>
                    </div>
                  </div>

                  {/* Card Risultati Statistici */}
                  <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                    <div className="p-4 rounded-xl bg-[#788070] text-white">
                      <div className="text-xs font-bold uppercase text-white/80">
                        Intervallo di Confidenza 95% (Pass Rate 95%)
                      </div>
                      <div className="text-3xl font-extrabold my-2">
                        {ciLow}% – {ciHigh}%
                      </div>
                      <p className="text-xs text-white/90">
                        {sampleSize <= 50
                          ? 'Campione piccolo: lo score oscilla fortemente per pochi casi!'
                          : sampleSize >= 350
                          ? 'Score stabile: permette di rilevare miglioramenti reali dal rumore.'
                          : 'Stabilità moderata: margine di incertezza ancora visibile.'}
                      </p>
                    </div>

                    <div className="p-4 rounded-xl border-2 border-amber-500/40 bg-amber-500/10">
                      <div className="text-xs font-bold uppercase text-amber-700 dark:text-amber-300">
                        Rare Failures: 0 Errori Osservati ≠ Rischio 0
                      </div>
                      <div className="text-3xl font-extrabold text-amber-600 dark:text-amber-400 my-2">
                        ≤ {zeroErrorUpperBound}%
                      </div>
                      <p className="text-xs opacity-85">
                        Limite superiore di rischio al 95% con <strong>0 errori osservati</strong> su {sampleSize} deal (~3% dopo 100 deal, &lt;1% dopo ~300 deal).
                      </p>
                    </div>
                  </div>
                </div>

                <div className="mt-6 pt-4 border-t border-gray-200 dark:border-gray-700 text-xs opacity-85">
                  <strong>Regola per i Release Criteria (Slide 19):</strong> Il limite inferiore dell'intervallo di confidenza al 95% deve essere <strong>almeno ≥ 90%</strong> per le bozze che superano tutti e 5 i criteri (su 400 deal con 95% di pass rate, l'intervallo è 92–97% → <strong>92% ≥ 90% OK!</strong>).
                </div>
              </div>
            </div>
          </div>
        )}

        {/* =====================================================================
            TAB 4: PRODUZIONE (OFFLINE VS ONLINE), DASHBOARD & NEXT STEPS (Slide 19-24)
           ===================================================================== */}
        {activeTab === 'production' && (
          <div className="space-y-8 animate-fadeIn">
            {/* MATRICE COSA RESTA IN SCOPE IN PRODUZIONE SENZA GROUND TRUTH (Slide 20) */}
            <div className={`p-6 rounded-2xl border shadow-sm ${cardBg}`}>
              <div className="flex flex-col md:flex-row md:items-center justify-between gap-4 mb-6">
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                    Slide 20 • Dal Benchmark Offline al Monitoraggio Online
                  </span>
                  <h2 className="text-xl font-bold mt-1">
                    Criteri in Produzione: Cosa possiamo controllare senza Ground Truth?
                  </h2>
                  <p className="text-xs opacity-75 mt-1">
                    In produzione <strong>manca il report QoE dell'esperto</strong>, ma abbiamo ancora: <strong>VDR, perimetro del task, policy di add-back, bozza dell'agente e trace di esecuzione</strong>.
                  </p>
                </div>

                <div className="flex gap-1.5 shrink-0">
                  {[
                    { id: 'both', label: 'Vista Comparativa' },
                    { id: 'online', label: 'Solo In Scope Online' },
                    { id: 'offline', label: 'Solo Richiede Ground Truth' }
                  ].map((m) => (
                    <button
                      key={m.id}
                      onClick={() => setProdViewMode(m.id)}
                      className={`px-3 py-1.5 rounded-lg text-xs font-bold border transition-all ${
                        prodViewMode === m.id
                          ? 'bg-[#788070] text-white border-[#788070]'
                          : 'border-gray-300 dark:border-gray-700 opacity-75'
                      }`}
                    >
                      {m.label}
                    </button>
                  ))}
                </div>
              </div>

              <div className="overflow-x-auto">
                <table className="w-full text-left border-collapse text-xs">
                  <thead>
                    <tr className="bg-[#788070] text-white uppercase tracking-wider text-[11px]">
                      <th className="p-3.5 rounded-tl-xl">Criterio</th>
                      {(prodViewMode === 'both' || prodViewMode === 'online') && (
                        <th className="p-3.5">Ancora in Scope Online (Con sola VDR e Policy)</th>
                      )}
                      {(prodViewMode === 'both' || prodViewMode === 'offline') && (
                        <th className="p-3.5 rounded-tr-xl">Fuori Scope Online senza Ground Truth (Richiede Audit Esperto)</th>
                      )}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-200 dark:divide-gray-700/70">
                    {Object.values(criteriaData).map((c) => (
                      <tr key={c.id} className="hover:bg-black/5 dark:hover:bg-white/5">
                        <td className="p-3.5 font-bold whitespace-nowrap">
                          <span className="px-2 py-0.5 rounded bg-[#788070]/20 text-[#788070] mr-2">
                            {c.id}
                          </span>
                          {c.name}
                        </td>
                        {(prodViewMode === 'both' || prodViewMode === 'online') && (
                          <td className="p-3.5 font-semibold text-emerald-700 dark:text-emerald-400">
                            ✓ {c.onlineScope}
                          </td>
                        )}
                        {(prodViewMode === 'both' || prodViewMode === 'offline') && (
                          <td className="p-3.5 opacity-80">
                            {c.id === 'C3' ? (
                              <span className="px-2 py-0.5 rounded bg-emerald-500/20 text-emerald-700 dark:text-emerald-300 font-bold">
                                {c.outOfScopeOnline}
                              </span>
                            ) : (
                              <span>⚠️ {c.outOfScopeOnline}</span>
                            )}
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>

            {/* FDD EVALUATION DASHBOARD (Slide 23) + CICLO DI PRODUZIONE (Slide 22) */}
            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
              {/* riproduzione interattiva della Slide 23: FDD Evaluation Dashboard */}
              <div className={`lg:col-span-8 p-6 rounded-2xl border shadow-sm ${cardBg}`}>
                <div className="flex items-center justify-between mb-4">
                  <div>
                    <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                      Slide 23 • FDD Evaluation Dashboard
                    </span>
                    <h3 className="text-lg font-bold">
                      Risultati Illustrativi: Benchmark Offline vs 1 Mese in Produzione
                    </h3>
                  </div>
                  <Activity className="w-5 h-5 text-[#788070]" />
                </div>

                <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
                  {/* Card Sinistra: OFFLINE BENCHMARK */}
                  <div className="p-5 rounded-2xl bg-[#788070] text-white flex flex-col justify-between">
                    <div>
                      <div className="text-[11px] font-bold uppercase tracking-wider text-white/80">
                        BENCHMARK / 400 INDEPENDENT DEALS
                      </div>
                      <div className="flex items-center gap-4 my-3">
                        <div className="text-5xl font-extrabold">95%</div>
                        <div className="text-xs text-white/90 leading-snug">
                          <strong>380 / 400</strong> superano tutti i 5 criteri
                          <br />
                          Intervallo 95%: <strong>92–97%</strong>
                        </div>
                      </div>

                      <div className="text-xs font-bold uppercase tracking-wider text-white/80 mt-4 mb-2">
                        Per-criterion pass rate
                      </div>
                      <div className="space-y-2.5 text-xs">
                        {[
                          { label: 'C1 Numeric accuracy', val: 99 },
                          { label: 'C2 Completeness', val: 98 },
                          { label: 'C3 Source support', val: 99 },
                          { label: 'C4 Analytical judgment', val: 97 },
                          { label: 'C5 Uncertainty handling', val: 98 }
                        ].map((item) => (
                          <div key={item.label}>
                            <div className="flex justify-between mb-1">
                              <span>{item.label}</span>
                              <span className="font-bold">{item.val}%</span>
                            </div>
                            <div className="w-full h-2 rounded-full bg-black/20 overflow-hidden">
                              <div
                                className="h-full bg-[#E8F0E4]"
                                style={{ width: `${item.val}%` }}
                              />
                            </div>
                          </div>
                        ))}
                      </div>
                    </div>

                    <div className="mt-5 pt-3 border-t border-white/20 text-[11px] text-white/90">
                      Tutte le 4 condizioni di rilascio (Slide 19) soddisfatte prima del go-live.
                    </div>
                  </div>

                  {/* Card Destra: ONLINE / 20 DRAFTS IN 30 DAYS */}
                  <div className="p-5 rounded-2xl border border-gray-200 dark:border-gray-700 flex flex-col justify-between bg-black/5 dark:bg-white/5">
                    <div>
                      <div className="text-[11px] font-bold uppercase tracking-wider opacity-60">
                        ONLINE / 20 DRAFTS IN 30 DAYS
                      </div>
                      <div className="text-sm font-bold mt-1 mb-4">
                        Controlli sulle evidenze (non correttezza completa del task)
                      </div>

                      <div className="space-y-4">
                        <div className="flex items-center justify-between p-3 rounded-xl bg-white dark:bg-[#1E2328] border border-gray-200 dark:border-gray-700">
                          <span className="text-xs font-medium">C1 Math checks pass</span>
                          <span className="text-2xl font-extrabold text-[#788070]">95%</span>
                        </div>

                        <div className="flex items-center justify-between p-3 rounded-xl bg-white dark:bg-[#1E2328] border border-gray-200 dark:border-gray-700">
                          <span className="text-xs font-medium">C3 Source checks pass</span>
                          <span className="text-2xl font-extrabold text-[#788070]">90%</span>
                        </div>

                        <div className="flex items-center justify-between p-3 rounded-xl bg-white dark:bg-[#1E2328] border border-gray-200 dark:border-gray-700">
                          <span className="text-xs font-medium">Escalated for correction (agli analisti)</span>
                          <span className="text-2xl font-extrabold text-amber-600">10%</span>
                        </div>
                      </div>
                    </div>

                    {/* Box Audit Esperto (Slide 23) */}
                    <div className="mt-4 p-4 rounded-xl bg-emerald-500/15 border border-emerald-500/30">
                      <div className="text-xs font-bold text-emerald-800 dark:text-emerald-300 flex items-center gap-1.5">
                        <Eye className="w-4 h-4" />
                        Expert Audit a Campione:
                      </div>
                      <p className="text-xs mt-1 opacity-90">
                        <strong>1 su 8 bozze promosse</strong> (passed drafts) presentava un'<strong>omissione materiale</strong> rilevata dagli esperti umani → aggiunta subito al benchmark offline (Slide 22)!
                      </p>
                    </div>
                  </div>
                </div>
              </div>

              {/* WORKSHEET 8: OPEN GAPS, OWNERS & NEXT STEP (Slide 24) */}
              <div className={`lg:col-span-4 p-6 rounded-2xl border shadow-sm flex flex-col justify-between ${cardBg}`}>
                <div>
                  <span className="text-xs font-bold uppercase tracking-wider text-[#788070]">
                    Slide 24 • Worksheet 8 Action Plan
                  </span>
                  <h3 className="text-lg font-bold mt-1 mb-3">
                    Open Gaps, Owners & Primo Passo Operativo
                  </h3>

                  <div className="space-y-2.5 text-xs">
                    {[
                      {
                        id: 'gap1',
                        piece: 'VDR access & usable volume',
                        action: 'Inventariare pacchetti finanziari completi',
                        owner: 'Data owner'
                      },
                      {
                        id: 'gap2',
                        piece: 'Validated adjustment references',
                        action: 'Audit di cifre, trattamento ed evidenze',
                        owner: 'Senior FDD experts'
                      },
                      {
                        id: 'gap3',
                        piece: 'Grader error rates',
                        action: 'Aggiudicare un pilota; validare su holdout',
                        owner: 'Eval owner'
                      },
                      {
                        id: 'gap4',
                        piece: 'Materiality limits & sample size',
                        action: 'Concordare soglie di rischio e numero deal',
                        owner: 'Business + eval owners'
                      },
                      {
                        id: 'gap5',
                        piece: 'Production checks & review capacity',
                        action: 'Validare grader; allocare staff di review',
                        owner: 'Operations owner'
                      }
                    ].map((item) => (
                      <div
                        key={item.id}
                        onClick={() =>
                          setCheckedGaps((prev) => ({ ...prev, [item.id]: !prev[item.id] }))
                        }
                        className={`p-3 rounded-xl border cursor-pointer transition-all flex items-start gap-2.5 ${
                          checkedGaps[item.id]
                            ? 'bg-emerald-500/10 border-emerald-500/40'
                            : 'border-gray-200 dark:border-gray-700 hover:border-[#788070]'
                        }`}
                      >
                        <input
                          type="checkbox"
                          checked={checkedGaps[item.id]}
                          onChange={() => {}}
                          className="mt-0.5 accent-[#788070]"
                        />
                        <div className="flex-1">
                          <div className="flex items-center justify-between gap-1">
                            <span className="font-bold">{item.piece}</span>
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-[#788070]/20 text-[#788070] font-semibold">
                              {item.owner}
                            </span>
                          </div>
                          <div className="opacity-75 mt-0.5">{item.action}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>

                <div className="mt-4 p-3.5 rounded-xl bg-[#788070] text-white text-xs">
                  <div className="font-bold uppercase tracking-wider text-[10px] text-white/80">
                    First Next Step (Slide 24):
                  </div>
                  <div className="font-semibold mt-0.5">
                    Inventariare le VDR utilizzabili e assemblare <strong>20 task completi</strong> con riferimenti validati dagli esperti.
                  </div>
                </div>
              </div>
            </div>
          </div>
        )}
      </main>
    </div>
  );
}