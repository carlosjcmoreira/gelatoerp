import React, { useState } from "react";

const PALETTE = [
  { name: "Primary", role: "Teal", hex: "#1A7A6E" },
  { name: "Secondary", role: "Coral", hex: "#E8614A" },
  { name: "Accent", role: "Yellow", hex: "#F5A623" },
  { name: "Mint", role: "Fresh", hex: "#2BB5A0" },
  { name: "Cream", role: "BG Light", hex: "#FFF8F2" },
  { name: "Dark BG", role: "BG Dark", hex: "#1E2B2A" },
  { name: "Charcoal", role: "Text", hex: "#1E1E1E" },
  { name: "Success", role: "Green", hex: "#3DAA68" },
  { name: "Danger", role: "Red", hex: "#D93025" },
];

function ScoopSVG({ size = 48, color1 = "#1A7A6E", color2 = "#FFF8F2" }: { size?: number; color1?: string; color2?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M24 6C16.268 6 10 12.268 10 20C10 24.1687 11.8217 27.913 14.708 30.5L24 42L33.292 30.5C36.1783 27.913 38 24.1687 38 20C38 12.268 31.732 6 24 6Z" fill={color1}/>
      <path d="M24 12C19.5817 12 16 15.5817 16 20C16 22.3846 17.0427 24.524 18.707 25.9868L24 32.5L29.293 25.9868C30.9573 24.524 32 22.3846 32 20C32 15.5817 28.4183 12 24 12Z" fill={color2}/>
    </svg>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="w-full">
      <div className="flex items-center gap-4 mb-6">
        <h2 className="text-xs uppercase tracking-widest font-bold text-neutral-400 whitespace-nowrap">{title}</h2>
        <div className="flex-1 h-px bg-neutral-200"></div>
      </div>
      {children}
    </section>
  );
}

export function ScoopyPresentation() {
  const [approvedLogo, setApprovedLogo] = useState<"modern" | "playful" | null>(null);

  return (
    <div className="min-h-screen bg-[#F4F4F2] p-10 space-y-14 font-['DM_Sans',sans-serif]">
      <style dangerouslySetInnerHTML={{__html:`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;700&family=Nunito:wght@700;800;900&display=swap');
      `}} />

      {/* Header */}
      <div className="text-center space-y-2">
        <div className="inline-flex items-center gap-2 mb-2">
          <ScoopSVG size={32} color1="#1A7A6E" color2="#FFF8F2" />
          <span className="text-3xl font-bold text-[#1E1E1E] tracking-tight">Scoopy — Brand Identity</span>
          <ScoopSVG size={32} color1="#E8614A" color2="#F5A623" />
        </div>
        <p className="text-sm text-neutral-500">Gelato & Pastelaria · Porto · Aprovação de identidade visual</p>
      </div>

      {/* LOGOS SIDE BY SIDE */}
      <Section title="01 — Variantes de Logótipo">
        <div className="grid grid-cols-2 gap-6">
          {/* Modern */}
          <div className={`rounded-2xl overflow-hidden border-4 transition-all duration-200 ${approvedLogo === 'modern' ? 'border-[#1A7A6E] shadow-lg shadow-[#1A7A6E]/20' : 'border-transparent'}`}>
            <div className="bg-[#FFF8F2] p-8 flex flex-col items-center gap-6">
              <div className="flex items-center gap-4">
                <svg width="52" height="52" viewBox="0 0 48 48" fill="none">
                  <path d="M24 6C16.268 6 10 12.268 10 20C10 24.1687 11.8217 27.913 14.708 30.5L24 42L33.292 30.5C36.1783 27.913 38 24.1687 38 20C38 12.268 31.732 6 24 6Z" fill="#1A7A6E"/>
                  <path d="M24 12C19.5817 12 16 15.5817 16 20C16 22.3846 17.0427 24.524 18.707 25.9868L24 32.5L29.293 25.9868C30.9573 24.524 32 22.3846 32 20C32 15.5817 28.4183 12 24 12Z" fill="#FFF8F2"/>
                </svg>
                <span className="text-5xl font-semibold text-[#1E1E1E] tracking-tight" style={{fontFamily:"'DM Sans', sans-serif"}}>Scoopy</span>
              </div>
              <div className="text-center space-y-1">
                <p className="text-xs uppercase tracking-widest text-[#1A7A6E] font-medium">Gelato & Pastelaria</p>
              </div>
            </div>
            <div className="bg-[#1E2B2A] p-6 flex items-center justify-center gap-4">
              <svg width="36" height="36" viewBox="0 0 48 48" fill="none">
                <path d="M24 6C16.268 6 10 12.268 10 20C10 24.1687 11.8217 27.913 14.708 30.5L24 42L33.292 30.5C36.1783 27.913 38 24.1687 38 20C38 12.268 31.732 6 24 6Z" fill="#2BB5A0"/>
                <path d="M24 12C19.5817 12 16 15.5817 16 20C16 22.3846 17.0427 24.524 18.707 25.9868L24 32.5L29.293 25.9868C30.9573 24.524 32 22.3846 32 20C32 15.5817 28.4183 12 24 12Z" fill="#FFF8F2"/>
              </svg>
              <span className="text-3xl font-semibold text-white tracking-tight" style={{fontFamily:"'DM Sans', sans-serif"}}>Scoopy</span>
            </div>
            <div className="bg-white p-4 flex items-center justify-between">
              <div>
                <p className="font-semibold text-sm text-neutral-800">Moderna & Clean</p>
                <p className="text-xs text-neutral-400">Premium · DM Sans · Teal #1A7A6E</p>
              </div>
              <button
                onClick={() => setApprovedLogo(approvedLogo === 'modern' ? null : 'modern')}
                className={`px-4 py-2 rounded-full text-sm font-semibold transition-all duration-150 ${approvedLogo === 'modern' ? 'bg-[#1A7A6E] text-white' : 'bg-neutral-100 text-neutral-600 hover:bg-neutral-200'}`}
              >
                {approvedLogo === 'modern' ? '✓ Aprovado' : 'Aprovar'}
              </button>
            </div>
          </div>

          {/* Playful */}
          <div className={`rounded-2xl overflow-hidden border-4 transition-all duration-200 ${approvedLogo === 'playful' ? 'border-[#E8614A] shadow-lg shadow-[#E8614A]/20' : 'border-transparent'}`}>
            <div className="bg-[#FFF8F0] p-8 flex flex-col items-center gap-6" style={{fontFamily:"'Nunito', sans-serif"}}>
              <div className="flex items-center gap-4">
                <svg width="52" height="52" viewBox="0 0 100 100" fill="none">
                  <circle cx="50" cy="42" r="28" fill="#E8614A"/>
                  <ellipse cx="50" cy="42" rx="16" ry="12" fill="#F5A623"/>
                  <rect x="35" y="65" width="30" height="24" rx="4" fill="#2BB5A0"/>
                  <path d="M35 65 Q50 55 65 65" fill="#F5A623"/>
                </svg>
                <span className="text-5xl font-black text-[#E8614A]">Scoopy</span>
              </div>
              <p className="text-xs uppercase tracking-widest text-[#2BB5A0] font-bold">Gelato & Pastelaria</p>
            </div>
            <div className="p-6 flex items-center justify-center gap-4" style={{background:"linear-gradient(135deg,#E8614A,#F5A623)"}}>
              <svg width="36" height="36" viewBox="0 0 100 100" fill="none">
                <circle cx="50" cy="42" r="28" fill="white" fillOpacity="0.3"/>
                <ellipse cx="50" cy="42" rx="16" ry="12" fill="white" fillOpacity="0.5"/>
                <rect x="35" y="65" width="30" height="24" rx="4" fill="white" fillOpacity="0.4"/>
              </svg>
              <span className="text-3xl font-black text-white" style={{fontFamily:"'Nunito', sans-serif"}}>Scoopy</span>
            </div>
            <div className="bg-white p-4 flex items-center justify-between">
              <div>
                <p className="font-semibold text-sm text-neutral-800">Lúdica & Colorida</p>
                <p className="text-xs text-neutral-400">Artesanal · Nunito · Coral #E8614A</p>
              </div>
              <button
                onClick={() => setApprovedLogo(approvedLogo === 'playful' ? null : 'playful')}
                className={`px-4 py-2 rounded-full text-sm font-semibold transition-all duration-150 ${approvedLogo === 'playful' ? 'bg-[#E8614A] text-white' : 'bg-neutral-100 text-neutral-600 hover:bg-neutral-200'}`}
              >
                {approvedLogo === 'playful' ? '✓ Aprovado' : 'Aprovar'}
              </button>
            </div>
          </div>
        </div>

        {approvedLogo && (
          <div className={`mt-4 p-4 rounded-xl text-sm font-semibold text-center ${approvedLogo === 'modern' ? 'bg-[#1A7A6E]/10 text-[#1A7A6E]' : 'bg-[#E8614A]/10 text-[#E8614A]'}`}>
            ✓ Variante aprovada: <strong>{approvedLogo === 'modern' ? 'Moderna & Clean' : 'Lúdica & Colorida'}</strong>
          </div>
        )}
      </Section>

      {/* ICON + PALETTE SIDE BY SIDE */}
      <Section title="02 — Ícone & Paleta de Cores">
        <div className="grid grid-cols-3 gap-6">
          {/* Icon */}
          <div className="bg-white rounded-2xl p-6 shadow-sm">
            <p className="text-xs uppercase tracking-widest text-neutral-400 font-semibold mb-4">Ícone da App</p>
            <div className="flex flex-col items-center gap-4">
              {[
                { size: 72, label: "72px" },
                { size: 48, label: "48px" },
                { size: 32, label: "32px" },
              ].map(({ size, label }) => (
                <div key={label} className="flex items-center gap-3">
                  <div style={{width:size,height:size,borderRadius:size*0.2,background:"linear-gradient(135deg,#1A7A6E,#2BB5A0)",display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0}}>
                    <svg width={size*0.6} height={size*0.6} viewBox="0 0 48 48" fill="none">
                      <path d="M24 4C18 4 13 9 13 15C13 18.8 14.8 22.1 17.5 24.3L24 34L30.5 24.3C33.2 22.1 35 18.8 35 15C35 9 30 4 24 4Z" fill="#FFF8F2"/>
                      <path d="M24 10C20.7 10 18 12.7 18 16C18 17.8 18.8 19.5 20 20.6L24 27L28 20.6C29.2 19.5 30 17.8 30 16C30 12.7 27.3 10 24 10Z" fill="#1A7A6E" fillOpacity="0.4"/>
                    </svg>
                  </div>
                  <span className="text-xs text-neutral-400 font-mono">{label}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Color Palette — full width */}
          <div className="col-span-2 bg-white rounded-2xl p-6 shadow-sm">
            <p className="text-xs uppercase tracking-widest text-neutral-400 font-semibold mb-4">Paleta de Cores</p>
            <div className="grid grid-cols-3 gap-4">
              {PALETTE.map(c => (
                <div key={c.hex} className="flex items-center gap-3">
                  <div className="w-12 h-12 rounded-xl shadow-sm border border-black/5 flex-shrink-0" style={{background:c.hex}}></div>
                  <div>
                    <p className="text-sm font-semibold text-neutral-800">{c.name}</p>
                    <p className="text-xs text-neutral-400">{c.role}</p>
                    <p className="text-xs font-mono text-neutral-500">{c.hex}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      </Section>

      {/* ADVERTISING SIDE BY SIDE */}
      <Section title="03 — Material Publicitário">
        <div className="grid grid-cols-2 gap-6">
          {/* Social Post Preview */}
          <div className="bg-white rounded-2xl p-6 shadow-sm">
            <p className="text-xs uppercase tracking-widest text-neutral-400 font-semibold mb-4">Post Instagram</p>
            <div className="aspect-square w-full max-w-xs mx-auto rounded-xl overflow-hidden" style={{background:"linear-gradient(145deg,#E8614A,#F5A623)"}}>
              <div className="h-full flex flex-col items-center justify-center p-8 gap-4">
                <svg width="100" height="120" viewBox="0 0 100 120" fill="none">
                  <ellipse cx="50" cy="52" rx="32" ry="30" fill="#FFF8F2"/>
                  <ellipse cx="50" cy="52" rx="18" ry="14" fill="#2BB5A0"/>
                  <ellipse cx="50" cy="52" rx="8" ry="6" fill="#1A7A6E"/>
                  <rect x="34" y="78" width="32" height="36" rx="5" fill="#E8614A"/>
                  <path d="M34 78 Q50 66 66 78" fill="#F5A623"/>
                  <circle cx="42" cy="50" r="4" fill="#F5A623" fillOpacity="0.7"/>
                  <circle cx="60" cy="48" r="3" fill="#E8614A" fillOpacity="0.6"/>
                </svg>
                <div className="text-center text-white">
                  <p className="text-xl font-black" style={{fontFamily:"'Nunito',sans-serif"}}>O Verão chegou</p>
                  <p className="text-lg font-black" style={{fontFamily:"'Nunito',sans-serif"}}>ao Scoopy! 🍦</p>
                  <p className="text-xs mt-2 opacity-80">Manga · Limão · Framboesa</p>
                </div>
                <p className="text-white/60 text-xs">@scoopy.porto</p>
              </div>
            </div>
          </div>

          {/* Business Card Preview */}
          <div className="bg-white rounded-2xl p-6 shadow-sm">
            <p className="text-xs uppercase tracking-widest text-neutral-400 font-semibold mb-4">Cartão de Visita</p>
            <div className="space-y-4">
              {/* Front */}
              <div className="relative w-full h-[130px] bg-[#FFF8F2] rounded-lg overflow-hidden flex shadow-md" style={{boxShadow:"0 4px 15px rgba(0,0,0,0.1)"}}>
                <div className="w-3 h-full bg-[#1A7A6E]"></div>
                <div className="flex-1 p-4 flex flex-col justify-between">
                  <div className="flex items-center gap-2">
                    <svg width="28" height="28" viewBox="0 0 100 100" fill="none"><circle cx="50" cy="50" r="50" fill="#F5A623"/><path d="M50 80 C 30 80 30 40 50 40 C 70 40 70 80 50 80 Z" fill="#E8614A"/><path d="M50 40 C 40 40 40 15 50 15 C 60 15 60 40 50 40 Z" fill="#FFF8F2"/></svg>
                    <div><p className="text-2xl font-black text-[#1A7A6E] leading-none" style={{fontFamily:"'Nunito',sans-serif"}}>Scoopy</p><p className="text-[8px] font-bold text-[#E8614A] uppercase tracking-widest">Gelato & Pastelaria</p></div>
                  </div>
                  <div className="text-right text-[10px] text-neutral-600 space-y-0.5">
                    <p>Rua das Flores, Porto</p>
                    <p className="font-semibold">@scoopy.porto · scoopy.pt</p>
                  </div>
                </div>
              </div>
              {/* Back */}
              <div className="relative w-full h-[130px] bg-[#1A7A6E] rounded-lg overflow-hidden flex items-center justify-center shadow-md" style={{boxShadow:"0 4px 15px rgba(0,0,0,0.1)"}}>
                <div className="absolute inset-0 opacity-20" style={{backgroundImage:`radial-gradient(#F5A623 2px, transparent 2px)`,backgroundSize:'25px 25px'}}></div>
                <div className="relative z-10 w-14 h-14 bg-[#FFF8F2] rounded-full flex items-center justify-center border-2 border-[#F5A623]">
                  <svg width="36" height="36" viewBox="0 0 100 100" fill="none"><path d="M50 80 C 30 80 30 40 50 40 C 70 40 70 80 50 80 Z" fill="#E8614A"/><path d="M50 40 C 40 40 40 15 50 15 C 60 15 60 40 50 40 Z" fill="#F5A623"/><circle cx="50" cy="15" r="8" fill="#1A7A6E"/></svg>
                </div>
              </div>
              {/* Palette strip */}
              <div className="flex gap-1 rounded-lg overflow-hidden h-6">
                {PALETTE.slice(0,6).map(c=><div key={c.hex} className="flex-1 group relative" style={{background:c.hex}} title={`${c.name} ${c.hex}`}></div>)}
              </div>
            </div>
          </div>
        </div>
      </Section>

      {/* APPROVAL SUMMARY */}
      <Section title="04 — Resumo de Aprovação">
        <div className={`rounded-2xl p-6 border-2 ${approvedLogo ? 'border-[#3DAA68] bg-[#3DAA68]/5' : 'border-neutral-200 bg-white'}`}>
          <div className="flex items-center justify-between">
            <div>
              <p className="font-bold text-neutral-800">
                {approvedLogo
                  ? `✓ Variante aprovada: ${approvedLogo === 'modern' ? 'Moderna & Clean' : 'Lúdica & Colorida'}`
                  : 'Nenhuma variante aprovada ainda'}
              </p>
              <p className="text-sm text-neutral-500 mt-1">
                {approvedLogo
                  ? 'Pronto para avançar para as tarefas #89 (Rebrand & Cores) e #90 (Navegação)'
                  : 'Selecione "Aprovar" na variante de logótipo preferida acima para continuar.'}
              </p>
            </div>
            <div className={`w-12 h-12 rounded-full flex items-center justify-center text-2xl ${approvedLogo ? 'bg-[#3DAA68]/20' : 'bg-neutral-100'}`}>
              {approvedLogo ? '✓' : '○'}
            </div>
          </div>
        </div>
      </Section>
    </div>
  );
}
