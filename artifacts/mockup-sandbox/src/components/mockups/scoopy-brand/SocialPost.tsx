import React from 'react';

export function SocialPost() {
  return (
    <div className="flex items-center justify-center min-h-screen bg-neutral-100 p-8 font-sans">
      <style dangerouslySetInnerHTML={{__html: `
        @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;700;800;900&display=swap');
        .font-nunito { font-family: 'Nunito', sans-serif; }
      `}} />
      
      {/* IG Post Container - 1:1 Aspect Ratio, 540x540px */}
      <div 
        className="relative w-[540px] h-[540px] overflow-hidden rounded-xl shadow-2xl flex flex-col font-nunito"
        style={{
          background: 'linear-gradient(135deg, #F5A623 0%, #E8614A 100%)',
          color: '#FFF8F2'
        }}
      >
        {/* Decorative background elements */}
        <div className="absolute top-[-50px] right-[-50px] w-64 h-64 bg-white/10 rounded-full blur-2xl pointer-events-none"></div>
        <div className="absolute bottom-[-50px] left-[-50px] w-80 h-80 bg-[#1A7A6E]/20 rounded-full blur-3xl pointer-events-none"></div>
        
        {/* Content Container */}
        <div className="relative z-10 flex flex-col items-center justify-between h-full p-10 text-center">
          
          {/* Header */}
          <div className="space-y-2 mt-4">
            <h2 className="text-xl font-bold uppercase tracking-widest text-[#FFF8F2]/90">
              Novos Sabores
            </h2>
            <h1 className="text-5xl font-black leading-tight drop-shadow-md">
              O Verão chegou<br/>ao Scoopy
            </h1>
          </div>
          
          {/* Center Illustration - Ice Cream */}
          <div className="relative flex items-center justify-center flex-1 my-6">
            <svg width="220" height="220" viewBox="0 0 200 300" fill="none" xmlns="http://www.w3.org/2000/svg" className="drop-shadow-xl transform hover:scale-105 transition-transform duration-500">
              {/* Cone */}
              <path d="M100 280 L40 140 L160 140 Z" fill="#E2A669" stroke="#C88541" strokeWidth="4"/>
              {/* Cone Grid Lines */}
              <path d="M50 160 L140 260 M70 140 L120 280 M130 140 L80 280 M150 160 L60 260" stroke="#C88541" strokeWidth="2" strokeLinecap="round"/>
              
              {/* Bottom Scoop - Raspberry */}
              <path d="M40 140 C 20 140 20 100 50 100 C 50 70 90 70 100 70 C 110 70 150 70 150 100 C 180 100 180 140 160 140 Z" fill="#E8614A"/>
              {/* Middle Scoop - Mango */}
              <path d="M50 100 C 30 100 30 60 60 60 C 60 30 100 30 110 30 C 120 30 140 30 140 60 C 170 60 170 100 150 100 Z" fill="#F5A623"/>
              {/* Top Scoop - Lemon */}
              <path d="M60 60 C 50 60 50 30 70 30 C 70 10 90 0 100 0 C 110 0 130 10 130 30 C 150 30 150 60 140 60 Z" fill="#FFF8F2"/>
              
              {/* Drips & Details */}
              <path d="M60 140 C 60 155 70 155 70 140" fill="#E8614A"/>
              <path d="M130 140 C 130 160 145 160 145 140" fill="#E8614A"/>
              <circle cx="90" cy="40" r="3" fill="#1A7A6E"/>
              <circle cx="110" cy="80" r="4" fill="#E8614A"/>
              <circle cx="70" cy="120" r="3" fill="#FFF8F2"/>
              <circle cx="130" cy="110" r="3" fill="#FFF8F2"/>
            </svg>
            
            {/* Sparkles */}
            <svg className="absolute top-10 left-0 animate-pulse" width="30" height="30" viewBox="0 0 24 24" fill="none">
              <path d="M12 0L14.59 9.41L24 12L14.59 14.59L12 24L9.41 14.59L0 12L9.41 9.41L12 0Z" fill="#FFF8F2"/>
            </svg>
            <svg className="absolute bottom-20 right-0 animate-pulse" width="20" height="20" viewBox="0 0 24 24" fill="none" style={{animationDelay: '0.5s'}}>
              <path d="M12 0L14.59 9.41L24 12L14.59 14.59L12 24L9.41 14.59L0 12L9.41 9.41L12 0Z" fill="#F5A623"/>
            </svg>
          </div>
          
          {/* Flavor Text */}
          <div className="bg-[#1E1E1E]/20 backdrop-blur-sm px-6 py-3 rounded-full mb-6 border border-white/20 shadow-lg">
            <p className="text-lg font-bold">
              Manga + Limão + Framboesa
            </p>
          </div>
          
          {/* Footer */}
          <div className="w-full flex justify-between items-end border-t border-white/20 pt-4">
            <div className="text-left">
              <p className="text-2xl font-black text-[#1A7A6E] drop-shadow-[0_1px_1px_rgba(255,255,255,0.8)]">Scoopy</p>
              <p className="text-xs font-semibold opacity-90 uppercase tracking-widest">Gelato & Pastelaria</p>
            </div>
            <div className="flex items-center gap-2 bg-[#1A7A6E] text-[#FFF8F2] px-4 py-2 rounded-full">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
                <path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 100 12.324 6.162 6.162 0 000-12.324zM12 16a4 4 0 110-8 4 4 0 010 8zm6.406-11.845a1.44 1.44 0 100 2.881 1.44 1.44 0 000-2.881z"/>
              </svg>
              <span className="font-bold text-sm">@scoopy.porto</span>
            </div>
          </div>
          
        </div>
      </div>
    </div>
  );
}
