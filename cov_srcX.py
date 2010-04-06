#! /usr/bin/env python
import aipy as a, numpy as n
import optparse, sys, math

o = optparse.OptionParser()
a.scripting.add_standard_options(o, cal=True, src=True, chan=True, 
    dec=True, ant=True, pol=True)
o.add_option('-d', '--dw', dest='dw', type=int, default=5,
    help='The number of delay bins to null. If -1, uses baseline lengths to generate a sky-pass filter.')
o.add_option('-r', '--drw', dest='drw', type=int, default=5,
    help='The number of delay-rate bins to null. If -1, uses baseline lengths to generate a sky-pass filter.')
o.add_option('--clean', dest='clean', type='float', default=1e-3,
    help='Deconvolve delay-domain data by the response that results from flagged data.  Specify a tolerance for termination (usually 1e-2 or 1e-3).')
o.add_option('--maxiter', dest='maxiter', type='int', default=100,
    help='Maximum number of clean iterations to allow.')
opts, args = o.parse_args(sys.argv[1:])

uv = a.miriad.UV(args[0])
aa = a.cal.get_aa(opts.cal, uv['sdf'], uv['sfreq'], uv['nchan'])
chans = a.scripting.parse_chans(opts.chan, uv['nchan'])
NCH = chans.size
aa.select_chans(chans)
afreqs = aa.get_afreqs()

srclist, cutoff, catalogs = a.scripting.parse_srcs(opts.src, opts.cat)
cat = a.cal.get_catalog(opts.cal, srclist, cutoff, catalogs)
NANT = len(aa)

for filename in args:
    # Gather data
    print 'Reading', filename
    msrdat, msrval, simdat = {}, {}, {}
    blwgt = {}
    antgain = {}
    curtime = None
    uv = a.miriad.UV(filename)
    a.scripting.uv_selector(uv, opts.ant, opts.pol)
    uv.select('decimate', opts.decimate, opts.decphs)
    for (crd,t,(i,j)),d,f in uv.all(raw=True):
        if t != curtime:
            simdat[t] = {}
            msrdat[t] = {}
            msrval[t] = {}
            blwgt[t] = {}
            aa.set_jultime(t)
            cat.compute(aa)
            srcs = [k for k in cat.keys() if cat[k].alt > 0]
            for k in srcs:
                simdat[t][k] = {}
                blwgt[t][k] = {}
            curtime = t
        #if not antgain.has_key(i): antgain[i] = n.sqrt(aa.passband(i,i))
        #if not antgain.has_key(j): antgain[j] = n.sqrt(aa.passband(j,j))
        if not antgain.has_key(i): antgain[i] = 1.
        if not antgain.has_key(j): antgain[j] = 1.
        bl = a.miriad.ij2bl(i,j)
        f = n.logical_not(f.take(chans)).astype(n.int)
        d = d.take(chans) * f
        msrdat[t][bl] = d
        msrval[t][bl] = f
        for k in srcs:
            simdat[t][k][bl] = n.conj(aa.gen_phs(cat[k],i,j,
                srcshape=cat[k].srcshape, resolve_src=True))
            u,v,w = aa.gen_uvw(i,j,cat[k]).squeeze()
            blwgt[t][k][bl] = f * n.sqrt(u**2 + v**2)

    # Compute initial residual
    resdat, curres, reswgt = {}, 0., 0.
    for t in msrdat:
        resdat[t] = {}
        for bl in msrdat[t]:
            resdat[t][bl] = msrdat[t][bl].copy()
            curres += n.sum(n.abs(resdat[t][bl])**2)
            reswgt += n.sum(msrval[t][bl])
    score = n.sqrt(curres / reswgt)
    print 'Initial Score:', score
    
    times = msrdat.keys(); times.sort()
    srcest = {}
    dw,drw = 0,0
    for iter in xrange(opts.maxiter):
        print 'Iteration', iter
        print '    DW=%d, DRW=%d' % (dw, drw)
        _srcest = {}
        for k in cat:
            # Beamform
            bmdat, bmwgt = [], []
            ts = [t for t in times if simdat[t].has_key(k)]
            if len(ts) == 0: continue
            for cnt, t in enumerate(ts):
                bmdat.append(0); bmwgt.append(0)
                for bl in simdat[t][k]:
                    _i,_j = a.miriad.bl2ij(bl)
                    bmdat[cnt] += resdat[t][bl] * n.conj(simdat[t][k][bl]) * blwgt[t][k][bl] / (antgain[_i] * antgain[_j])
                    bmwgt[cnt] += n.abs(simdat[t][k][bl])**2 * blwgt[t][k][bl]
            bmdat, bmwgt = n.array(bmdat), n.array(bmwgt)
            bmdat /= n.where(bmwgt > 0, bmwgt, 1)
            d = n.fft.ifft(bmdat, axis=1) # Delay Transform
            padlen = math.ceil(d.shape[0] * .2)
            d = n.concatenate([n.flipud(d[:padlen]),d,n.flipud(d[-padlen:])])
            d = n.fft.ifft(d, axis=0) # Delay-Rate Transform
            # Apply DDR Filter
            if iter < opts.maxiter - 5:
                x1,x2 = drw, -drw
                if x2 == 0: x2 = d.shape[0]
                d[x1+1:x2] = 0
            if iter < opts.maxiter - 5:
                y1,y2 = dw, -dw
                if y2 == 0: y2 = d.shape[1]
                d[:,y1+1:y2] = 0
            d = n.fft.fft(d, axis=0) # undo Delay-Rate Transform
            d = d[padlen:-padlen]
            d = n.fft.fft(d, axis=1) # undo Delay Transform
            d = d.real
    
            # Derive source estimators
            CLEAN_GAIN = .2
            _srcest[k] = {}
            print '    + <%f> to %s (drms=%f)' % (CLEAN_GAIN*n.median(d), k, CLEAN_GAIN*n.sqrt(n.average(d**2)))
            for cnt, t in enumerate(ts):
                _srcest[k][t] = srcest.get(k,{}).get(t,0) + CLEAN_GAIN*d[cnt]
    
        _resdat = {}
        newres, reswgt = 0., 0.
        # Remove new model
        for t in msrdat:
            _resdat[t] = {}
            for bl in msrdat[t]:
                _i,_j = a.miriad.bl2ij(bl)
                _resdat[t][bl] = msrdat[t][bl].copy()
                for k in _srcest:
                    if not _srcest[k].has_key(t): continue
                    _resdat[t][bl] -= simdat[t][k][bl] * _srcest[k][t] * antgain[_i] * antgain[_j]
                _resdat[t][bl] *= msrval[t][bl]
                newres += n.sum(n.abs(_resdat[t][bl])**2 * msrval[t][bl])
                reswgt += n.sum(msrval[t][bl])
        _score = n.sqrt(newres / reswgt)
        print '    New Score:', _score
        # Always accept any downhill jump, stop if bottoming out
        if _score > score:
            print 'Divergence'
            break
        elif 1 - _score/score < opts.clean:
            # Update antenna gains here?
            _antgain,antwgt = {}, {}
            import pylab as p
            for t in msrdat:
                blgain = {}
                for bl in msrdat[t]:
                    _i,_j = a.miriad.bl2ij(bl)
                    simd = 0.
                    for k in srcest:
                        if not srcest[k].has_key(t): continue
                        simd += simdat[t][k][bl] * srcest[k][t]
                    blgain[bl] = msrdat[t][bl] / (antgain[_i] * antgain[_j]) / simd * msrval[t][bl]
                    blgain[bl] = blgain[bl].clip(.25, 4)
                    #p.plot(blgain[bl])
                #p.show()
                #p.clf()
                for _i in range(NANT):
                  for _j in range(NANT):
                    if _i == _j: continue
                    elif _i < _j:
                        blij = a.miriad.ij2bl(_i,_j)
                        if not blgain.has_key(blij): continue
                        g_ij,w_ij = blgain[blij], msrval[t][blij]
                    else:
                        blji = a.miriad.ij2bl(_j,_i)
                        if not blgain.has_key(blji): continue
                        g_ij,w_ij = n.conj(blgain[blji]), msrval[t][blji]
                    for _k in range(NANT):
                        if _i == _k: continue
                        elif _i < _k:
                            blik = a.miriad.ij2bl(_i,_k)
                            if not blgain.has_key(blik): continue
                            g_ik,w_ik = blgain[blik], msrval[t][blik]
                        else:
                            blki = a.miriad.ij2bl(_k,_i)
                            if not blgain.has_key(blki): continue
                            g_ik,w_ik = n.conj(blgain[blki]), msrval[t][blki]
                        if _j == _k: continue
                        elif _j < _k:
                            bljk = a.miriad.ij2bl(_j,_k)
                            if not blgain.has_key(bljk): continue
                            g_jk,w_jk = blgain[bljk], msrval[t][bljk]
                        else:
                            blkj = a.miriad.ij2bl(_k,_j)
                            if not blgain.has_key(blkj): continue
                            g_jk,w_jk = n.conj(blgain[blkj]), msrval[t][blkj]
                        w_ii = w_ij*w_ik*w_jk
                        g_ii = n.where(w_ii > 0, n.conj(g_ij) * g_ik / g_jk, 0)
                        _antgain[_i] = _antgain.get(_i,0.) + n.abs(g_ii)
                        antwgt[_i] = antwgt.get(_i,0.) + w_ii
            for _i in antgain:
                _antgain[_i] /= n.where(antwgt[_i] == 0, 1, antwgt[_i])
                _antgain[_i] = n.sqrt(n.abs(_antgain[_i])).clip(.25,4)
                poly = n.polyfit(afreqs, _antgain[_i], deg=3)
                p.semilogy(_antgain[_i], ',')
                _antgain[_i] = n.polyval(poly, afreqs)
                p.semilogy(_antgain[_i], '-')
            avg = sum([_antgain[_i] for _i in antgain]) / len(antgain)
            for _i in antgain:
                antgain[_i] *= _antgain[_i]
                #print _i, _antgain[_i] / avg
            _resdat = {}
            newres, reswgt = 0., 0.
            # Remove new model
            for t in msrdat:
                _resdat[t] = {}
                for bl in msrdat[t]:
                    _i,_j = a.miriad.bl2ij(bl)
                    _resdat[t][bl] = msrdat[t][bl].copy()
                    for k in _srcest:
                        if not _srcest[k].has_key(t): continue
                        _resdat[t][bl] -= simdat[t][k][bl] * _srcest[k][t] * antgain[_i] * antgain[_j]
                    _resdat[t][bl] *= msrval[t][bl]
                    newres += n.sum(n.abs(_resdat[t][bl])**2 * msrval[t][bl])
                    reswgt += n.sum(msrval[t][bl])
            _score = n.sqrt(newres / reswgt)
            print '    New Score:', _score
            p.show()
                
            if dw == opts.dw and drw == opts.drw:
                if  1-_score/score < opts.clean:
                    print 'Tolerance'
                    break
            else:
                dw = min(dw+1, opts.dw)
                drw = min(drw+1, opts.drw)
        resdat,score,srcest = _resdat,_score,_srcest
      
    srctimes = {}
    for k in srcest:
        ts = srcest[k].keys()
        ts.sort()
        srctimes[k] = ts
        srcest[k] = n.array([srcest[k][t] for t in ts])
    n.savez( '%s__srcdata.npz' % (filename), **srcest)
    n.savez('%s__srctimes.npz' % (filename), **srctimes)
    n.savez('%s__srcfreqs.npz' % (filename), freqs=afreqs)
    #n.savez('%s__caldata.npz' % (filename), **caldata)

