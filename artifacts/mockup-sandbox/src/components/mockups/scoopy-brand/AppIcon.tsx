import React from 'react';

const ScoopyLogo = ({ className = "" }: { className?: string }) => (
  <svg viewBox="0 0 512 512" className={className} fill="none" xmlns="http://www.w3.org/2000/svg">
    {/* Base square with rounded corners */}
    <rect width="512" height="512" rx="112" fill="#E8614A" />
    
    {/* Stylized Ice Cream Cone / S shape */}
    {/* Cone */}
    <path d="M256 380L176 220H336L256 380Z" fill="#F5A623" />
    <path d="M256 380L176 220H256V380Z" fill="#E09612" /> {/* Cone shadow */}
    
    {/* Scoop (Top) */}
    <circle cx="256" cy="180" r="80" fill="#FFF8F2" />
    {/* Mint leaf / drip accent */}
    <path d="M280 120C290 100 320 100 330 120C340 140 310 160 280 120Z" fill="#2BB5A0" />
    <path d="M236 100C210 100 190 120 190 146C190 172 236 180 236 180C236 180 282 172 282 146C282 120 262 100 236 100Z" fill="#1A7A6E" />
    
    {/* "S" stylized swirl inside the scoop */}
    <path d="M280 150C280 135 256 135 256 150C256 165 232 165 232 180C232 195 256 195 256 180" stroke="#E8614A" strokeWidth="16" strokeLinecap="round" />
  </svg>
);

const LogoSizeSet = ({ bgClass, titleClass }: { bgClass: string, titleClass: string }) => (
  <div className={`p-8 rounded-2xl flex flex-col items-center gap-8 ${bgClass}`}>
    <div className="flex items-end justify-center gap-8">
      <div className="flex flex-col items-center gap-3">
        <ScoopyLogo className="w-[128px] h-[128px]" />
        <span className={`text-sm font-medium ${titleClass}`}>128px</span>
      </div>
      <div className="flex flex-col items-center gap-3">
        <ScoopyLogo className="w-[64px] h-[64px]" />
        <span className={`text-sm font-medium ${titleClass}`}>64px</span>
      </div>
      <div className="flex flex-col items-center gap-3">
        <ScoopyLogo className="w-[32px] h-[32px]" />
        <span className={`text-sm font-medium ${titleClass}`}>32px</span>
      </div>
      <div className="flex flex-col items-center gap-3">
        <ScoopyLogo className="w-[16px] h-[16px]" />
        <span className={`text-sm font-medium ${titleClass}`}>16px</span>
      </div>
    </div>
  </div>
);

export function AppIcon() {
  return (
    <div className="min-h-screen bg-[#FFF8F2] flex items-center justify-center p-8 font-sans">
      <div className="max-w-3xl w-full bg-white rounded-3xl shadow-xl overflow-hidden border border-[#E8614A]/10">
        <div className="p-8 border-b border-[#E8614A]/10 text-center">
          <h1 className="text-3xl font-bold text-[#1E1E1E] mb-2" style={{ fontFamily: 'Nunito, sans-serif' }}>App Icon</h1>
          <p className="text-[#1A7A6E]">Scoopy Artisan Gelato</p>
        </div>
        
        <div className="p-12 space-y-12 bg-slate-50">
          <div>
            <h2 className="text-sm uppercase tracking-widest text-slate-500 mb-6 font-semibold">Light Context</h2>
            <LogoSizeSet bgClass="bg-[#FFF8F2] shadow-sm border border-slate-100" titleClass="text-[#1E1E1E]" />
          </div>
          
          <div>
            <h2 className="text-sm uppercase tracking-widest text-slate-500 mb-6 font-semibold">Dark Context</h2>
            <LogoSizeSet bgClass="bg-[#1E2B2A] shadow-inner" titleClass="text-[#FFF8F2]" />
          </div>
        </div>
      </div>
    </div>
  );
}

export default AppIcon;
