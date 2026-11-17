// Every model and user text goes in through textContent: the CSP forbids inline code,
// and model output is untrusted. No text is kept in browser storage (health data).

export const TEXT = Object.freeze({
  coach: "Treinador:",
  you: "Você:",
  loading: "Carregando…",
  garminConnected: "Garmin: conectado",
  garminNotConnected: "Garmin: não conectado",
  garminLogin: "Conectar o Garmin",
  garminWaiting: "Garmin: esperando código",
  unsaved: "Mensagem não salva.",
  noResponse: "Sem resposta do servidor. Verificando…",
  maybeLost: "Sua mensagem pode não ter chegado ao treinador. Confira e envie de novo.",
  invalid: "Mensagem inválida.",
  serverError: "Algo deu errado no servidor. Tente de novo.",
  placeholderOnboarding: "Escreva sua resposta",
  placeholderAnswer: "Digite sua resposta",
  placeholderCheckinChat: "Escreva para o treinador",
  weekOneHint: "Seu plano está salvo. Falta enviar a semana 1 ao Garmin.",
  planNone: "Nenhum plano ainda. Faça o onboarding na Conversa para montar o plano.",
  planWeekOnePending: "A semana 1 ainda não foi enviada.",
  planNoWeek: "Nenhuma semana planejada agora. A próxima semana é planejada no domingo às 17h.",
  checkinDivider: "Check-in",
  checkinCanceled: "Check-in cancelado. Nada foi salvo.",
  thisWeek: "Esta semana",
  nextWeek: "Próxima semana",
  easy: "Leve",
  hard: "Forte",
  today: "Hoje",
  scheduled: "No relógio",
  notScheduled: "Ainda não está no relógio",
  weekEmpty: "Nenhum treino nesta semana.",
  yes: "Sim",
  no: "Não",
  mfaRequestedAt: "Pedido às ",
});

export const JOB_TEXT = Object.freeze({
  onboarding_turn: "O treinador está escrevendo…",
  checkin_reply: "O treinador está escrevendo…",
  week_one: "Gerando a semana 1 e enviando ao Garmin. Pode levar alguns minutos.",
  checkin_save: "Salvando o check-in…",
  onboarding_session: "Abrindo a sessão…",
  garmin_login: "Entrando no Garmin…",
  garmin_mfa: "Enviando o código ao Garmin…",
  onboarding_discard: "Descartando a sessão…",
  checkin_end: "Encerrando o check-in…",
});

export const UNSAVED_ONBOARDING = new Set(["message_too_long", "session_too_long", "model_failed", "macro_rejected"]);
export const ENDING_CHECKIN = new Set(["conversation_too_long", "model_failed", "not_open"]);
export const POLL_MS = 3000;
export const TICK_MS = 15000;
export const LATER_AFTER_MS = 300000;
export const FINE_POINTER = window.matchMedia("(pointer: fine)");
export const REDUCED_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)");
export const DAY_FORMAT = new Intl.DateTimeFormat("pt-BR", { weekday: "long", day: "2-digit", month: "2-digit" });
export const TIME_FORMAT = new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" });
