import React from 'react';

export function BusinessCard() {
  return (
    <div className="flex flex-col md:flex-row items-center justify-center min-h-screen bg-neutral-200 p-8 gap-12 font-sans">
      <style dangerouslySetInnerHTML={{__html: `
        @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800;900&display=swap');
        .font-nunito { font-family: 'Nunito', sans-serif; }
        
        .card-shadow {
          box-shadow: 0 20px 40px rgba(0,0,0,0.1), 0 5px 15px rgba(0,0,0,0.05);
        }
      `}} />
      
      {/* 
        Standard Business Card Proportion: 90x55mm 
        We'll use a larger scaled version for the mockup (approx 450x275px)
      */}
      
      {/* FRONT OF CARD */}
      <div className="group relative w-[450px] h-[275px] bg-[#FFF8F2] rounded-lg card-shadow overflow-hidden flex font-nunito transition-transform hover:-translate-y-2 hover:rotate-1 duration-300">
        
        {/* Left Color Accent Bar */}
        <div className="w-4 h-full bg-[#1A7A6E]"></div>
        
        <div className="flex-1 p-8 flex flex-col justify-between">
          {/* Logo Area */}
          <div className="flex items-center gap-3">
            <svg width="40" height="40" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
              <circle cx="50" cy="50" r="50" fill="#F5A623"/>
              <path d="M50 80 C 30 80 30 40 50 40 C 70 40 70 80 50 80 Z" fill="#E8614A"/>
              <path d="M50 40 C 40 40 40 15 50 15 C 60 15 60 40 50 40 Z" fill="#FFF8F2"/>
            </svg>
            <div>
              <h1 className="text-4xl font-black text-[#1A7A6E] leading-none tracking-tight">Scoopy</h1>
              <p className="text-[10px] font-bold text-[#E8614A] uppercase tracking-[0.2em] mt-1">Gelato & Pastelaria</p>
            </div>
          </div>
          
          {/* Contact Info */}
          <div className="flex flex-col gap-2 mt-8 text-right self-end">
            <div className="flex items-center justify-end gap-2 text-[#1E1E1E]">
              <span className="font-semibold text-sm">Rua das Flores, Porto</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#E8614A" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path>
                <circle cx="12" cy="10" r="3"></circle>
              </svg>
            </div>
            
            <div className="flex items-center justify-end gap-2 text-[#1E1E1E]">
              <span className="font-bold text-sm">@scoopy.porto</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#E8614A" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <rect x="2" y="2" width="20" height="20" rx="5" ry="5"></rect>
                <path d="M16 11.37A4 4 0 1 1 12.63 8 4 4 0 0 1 16 11.37z"></path>
                <line x1="17.5" y1="6.5" x2="17.51" y2="6.5"></line>
              </svg>
            </div>
            
            <div className="flex items-center justify-end gap-2 text-[#1A7A6E]">
              <span className="font-bold text-sm">scoopy.pt</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#1A7A6E" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="2" y1="12" x2="22" y2="12"></line>
                <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"></path>
              </svg>
            </div>
          </div>
        </div>
      </div>
      
      {/* BACK OF CARD */}
      <div className="group relative w-[450px] h-[275px] bg-[#1A7A6E] rounded-lg card-shadow overflow-hidden flex items-center justify-center transition-transform hover:-translate-y-2 hover:-rotate-1 duration-300">
        {/* Pattern Background */}
        <div className="absolute inset-0 opacity-20" style={{
          backgroundImage: `radial-gradient(#F5A623 3px, transparent 3px), radial-gradient(#E8614A 3px, transparent 3px)`,
          backgroundSize: '40px 40px',
          backgroundPosition: '0 0, 20px 20px'
        }}></div>
        
        {/* Wavy Swirl Element */}
        <div className="absolute w-full h-full opacity-30 pointer-events-none">
          <svg viewBox="0 0 450 275" preserveAspectRatio="none" width="100%" height="100%">
            <path d="M0,137.5 C112.5,275 337.5,0 450,137.5" fill="none" stroke="#FFF8F2" strokeWidth="20" strokeLinecap="round" strokeDasharray="1 30"/>
            <path d="M0,80 C112.5,217.5 337.5,-57.5 450,80" fill="none" stroke="#F5A623" strokeWidth="10" strokeLinecap="round" strokeDasharray="1 20"/>
            <path d="M0,195 C112.5,332.5 337.5,57.5 450,195" fill="none" stroke="#E8614A" strokeWidth="15" strokeLinecap="round" strokeDasharray="1 25"/>
          </svg>
        </div>
        
        {/* Central Logo Mark */}
        <div className="relative z-10 w-24 h-24 bg-[#FFF8F2] rounded-full flex items-center justify-center p-2 shadow-xl border-4 border-[#F5A623]">
          <svg width="60" height="60" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M50 80 C 30 80 30 40 50 40 C 70 40 70 80 50 80 Z" fill="#E8614A"/>
            <path d="M50 40 C 40 40 40 15 50 15 C 60 15 60 40 50 40 Z" fill="#F5A623"/>
            <circle cx="50" cy="15" r="8" fill="#1A7A6E"/>
          </svg>
        </div>
      </div>
      
    </div>
  );
}
