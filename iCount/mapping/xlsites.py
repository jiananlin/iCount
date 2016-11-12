"""
Identify and quantify cross-linked sites
----------------------------------------

Determine positions and quantity of cross-link events.

The simplest version of this script would oprate on such example::

    |--a---b--- reference sequence, chr 14, positive strand ------------
        |rbc1---R1--------|
        |rbc1---R2------|
        |rbc2---R3------|
            |rbc1--------R4-------------|
            |rbc3------R5-----|

Five reads (R1-R5) are mapped to a reference sequence (chromosome 14, positive
strand). Reads start on two distinct positons. On first position, there is
R1-R3. Cross-link site is located one nucleotide before start of the read (on
negative strand, one nucleotide after end of read). However, we wish to count
number of cDNA molecules, not the number of reads. This can be done by counting
the number of distinct random barcodes (sometimes also called randomers). So in
upper example, we have:

    Postion a: 3 reads, 2 distinct random barcodes = 2 cDNA's
    Postion b: 2 reads, 2 distinct random barcodes = 2 cDNA's

However, things can get complicated when a single read is mapped in multiple
parts. This can happen for several reasons. One common example is that introns
are removed during transcription. This can be illustrated with the following
image::

    |---------------- reference -----------------------

    |--------------------------transcript--------------------------|
    |---UTR5---||---intron---||---exon---||---intron---||---exon---|

                                |-------R1-------|
                                |--R2.1-->              <-R2.2-|
                      |-R3.1->      <-R3.2->            <-R3.3-|
                        |-R4.1->      <-R4.2-|

Read R1 and R2 are starting on same position. For the sake of argument, let's
also pretend they also have same random barcode. In so, we would count them as a
single cDNA molecule (= single cross-link event), even though it is obvious that
they represent two separate cross-link events. In order to fix this, we count
not just the number of different randomers on same position, but also number of
different "second-start" coordinates. Second-start coordinate is just the
coordinate of the second part of the read. This way, the actual number of
cross-link events can be determined more accurately. If read is not split, it's
second-start coordinate is 0. If read has multiple "holes" (as read R3) we
determine second-start from the largest hole.

Reads whose second-start do NOT fall on segmentation (like R4) are stored in a
separate BAM file ``sites_strange``. They should be treated with special care,
since they can indicate not-yet annotated features in genome. If segmentation is
not given, all reads with holes, bigger than ``holesize_th`` are considered
strange.

Another parameter needs more explanation: ``group_by``. When algorithm starts,
reads from BAM file are grouped in hierarchical structure by::

    * chromosome and strand
    * cross-link position
    * random barcode
    * second-start

Each second-start group receives 1 cDNA score. This score is divided to each
read in group (if there are 5 reads in group, each one gets 1/5 score). This
enables that each read has it's cDNA score and of course, 1 "read score". This
scores can be assigned to start (actually, to cross-link position), midlle or
end position of read. By default, score is of course assigned to cross-link
location. But for diagnostic purpuses, scores can also be assigned to middle or
end coordinate of the read.



TODO: check overlap between unique and multimap BED files, should be small,
otherwise, we should think of a more approapriate quantification of (division
of randomers among) unique mapped sites that overlap with multimapped reads

"""

import tempfile
import logging

import pysam

import iCount


LOGGER = logging.getLogger(__name__)
VALID_NUCLEOTIDES = set('ATCGN')


def _get_random_barcode(query_name, metrics):
    """
    Extract random barcode from ``query_name``.
    """
    if ':rbc:' in query_name:
        bc = query_name.rsplit(':rbc:', 1)[1].split(':')[0]
    elif ':' in query_name:
        bc = query_name.rsplit(':', 1)[1]
        if set(bc) - VALID_NUCLEOTIDES:
            # invalid barcode characters
            bc = ''
            metrics.invalidrandomer_recs += 1
    else:
        bc = ''
        metrics.norandomer_recs += 1

    return bc


def _match(s1, s2, mismatches):
    """
    Do sequence `s1` and `s2` have less or equal than ``mismatches``?

    Parameters
    ----------
    s1 : str
        First sequence.
    s2 : str
        Second sequence.
    mismatches : int
        Number of allowed mismatches between given sequences.

    Returns
    -------
    bool
        Do sequence `s1` and `s2` have less or equal than ``mismatches``

    """
    s1, s2 = s1.upper(), s2.upper()
    cn = sum([(c1 == 'N' or c2 == 'N' or c1 == c2) for c1, c2 in zip(s1, s2)])
    return max(len(s1), len(s2)) - cn <= mismatches


def _update(cur_vals, to_add):
    """
    Add the values from `to_add` to appropriate place in `cur_vals`

    Note: cur_vals is updated in place!

    Parameters
    ----------
    to_add : dict
        Dict with update data.

    Returns
    -------
    None
        None.

    """
    for pos, vals_to_add in to_add.items():
        prev_vals = cur_vals.get(pos, [0]*len(vals_to_add))
        cur_vals[pos] = [p + n for p, n in zip(prev_vals, vals_to_add)]


def _merge_similar_randomers(by_bc, mismatches):
    """
    Merge randomers on same site that are max ``mismatches`` different

    Input parameter `by_bc` has te following structure:
    by_bc = {
        'AAA': [(middle_pos, end_pos, read_len, num_mapped, second_start),  # hit1
                (middle_pos, end_pos, read_len, num_mapped, second_start),  # hit2
                (middle_pos, end_pos, read_len, num_mapped, second_start),  # ...
        ]
        'AAT': [(middle_pos, end_pos, read_len, num_mapped, second_start),  # hit1
                (middle_pos, end_pos, read_len, num_mapped, second_start),  # hit2
        ]

    Steps in function:
        0. Indentify ambigious randomers ('N' characters in barcode)

        1. For each ambigious randomer, identify similar non-ambigious one. If
        match is found, move hits form ambigious to the non-ambigious one. If no
        match is found, declare ambigious randomer (one that has 'N's) as
        non-ambigious anyway.

        2. For each barcode, identify if there exists any similar one. If
        there is, join the hits form second barcode to the first one.


    TODO: Code should be improved in step #1. Instead of finding any match,
    match with least difference should be found. Check also the skipped unit
    test in tests/test_xlsites.py

    Parameters
    ----------
    by_bc : dict
        Dictionary of barcodes and their hits.
    mismatches : int
        Reads on same position with random barcode differing less than
        ``mismatches`` are grouped together.

    Returns
    -------
    None
        None, since input `by_bc` is modified in-place.

    """
    # assign ambigious randomers to unambigious randomers
    accepted_bcs = set()  # accepted_barcodes
    ambig_bcs = []  # ambigious_barcodes
    for barcode in by_bc.keys():
        Ns = barcode.count('N')
        if Ns == 0:
            accepted_bcs.add(barcode)
        else:
            ambig_bcs.append((Ns, barcode))

    # Step #1:
    # For each ambigious randomer, identify similar non-ambigious one. If
    # match is found, move hits form ambigious to the non-ambigious one. If no
    # match is found, declare ambigious randomer (even if it has 'N's) as
    # non-ambigious anyway.
    for _, amb_bc in sorted(ambig_bcs):
        matches = False
        # Get list of accepted barcodes (sorted by decreasing frequency).
        # Sort is required each time as frequency changes when hits are
        # assigned from ambigious to unambigious randomer
        order_bcs = sorted([(len(hits), bc) for bc, hits in by_bc.items() if
                            bc in accepted_bcs], reverse=True)

        for _, bc in order_bcs:
            if _match(bc, amb_bc, mismatches):
                matches = True
                by_bc[bc].extend(by_bc.pop(amb_bc))
                break
        if not matches:
            accepted_bcs.add(amb_bc)

    # Step #2:
    # For each barcode, identify if there exists any similar one. If
    # there is, join the hits form second barcode to the first one.
    merged = True
    while merged:
        # start with most frequent randomers first
        order_bcs = [(len(hits), bc) for bc, hits in by_bc.items()]
        order_bcs = sorted(order_bcs, reverse=True)
        merged = False
        for i, (_, bc) in enumerate(order_bcs):
            for _, bc2 in order_bcs[i + 1:]:
                if _match(bc, bc2, mismatches):
                    merged = True
                    by_bc[bc].extend(by_bc.pop(bc2))
            if merged:
                break


def _collapse(xlink_pos, by_bc, group_by, multimax=1):
    """
    Report number of cDNAs and reads in cross-link site on xlink_pos

    Input parameter `by_bc` has te following structure:
    by_bc = {
        'AAA': [(middle_pos, end_pos, read_len, num_mapped, cigar, second_start),  # hit1
                (middle_pos, end_pos, read_len, num_mapped, cigar, second_start),  # hit2
                (middle_pos, end_pos, read_len, num_mapped, cigar, second_start),  # ...
        ]
        'AAT': [(middle_pos, end_pos, read_len, num_mapped, cigar, second_start),  # hit1
                (middle_pos, end_pos, read_len, num_mapped, cigar, second_start),  # hit2
                ]

    Counting the number of reads is easy - just count the number of hits per
    cross-link site.

    Counting the number of cDNAs is also easy - just count the number of
    different barcodes. However, following scenarions also need to be handled:

        * one read ban be mapped to multiple sites. In this case, the
          "contribution" of such read has to be divided equally to all positions
          that it maps to.
        * Also, longer reads should have proportionally greater "contribution".
          than the short ones.

    Upper two scenarions imply that each read contributes::

        weight = 1 * 1/a * b/c
        # a = number of hits
        # b = read length
        # c = sum(read lengths per same barcode)

    Another factor to take into account is also the possibility that a group of
    reads with equal start position and barcode represents multiple cross-links.
    Imagine a read starting 10 bp before exon-intron junction. One group of
    reads maps in the intron section and other reads skip the intron and map on
    next exon with the second part of the read. This can be solved by grouping
    by "second_start", which is the coordinate of the first nucleotide of the
    second part of the read. Each group with unique second start is treated as
    an independent cross-link event. This is done in function
    ``_separate_by_second_starts``

    Returns an object ``counts``::

        counts = {
            position: [cDNA_count, reads_count],
            123: [3.14, 42],
            124: [5.79, 16],
            ...
        }

    Parameters
    ----------
    xlink_pos : int
        Cross link position (genomic coordinate).
    by_bc : dict
        Dict with hits for each barcode.
    group_by : str
        Report by start, middle or end position.
    multimax : int
        Ignore reads, mapped to more than ``multimax`` places.

    Returns
    -------
    dict
        Number of cDNA and reads for each position.

    """
    gi = ['start', 'middle', 'end'].index(group_by)

    # Container for cDNA and read counts:
    counts = {}

    for bc, hits in by_bc.items():

        # separate in groups by second-start
        ss_groups = {}
        for read in hits:
            ss_groups.setdefault(read[4], []).append(read)

        for ss_group in ss_groups.values():

            # Sum of all read lengths per ss_group:
            sum_len_per_barcode = sum([i[2] for i in ss_group if i[3] <= multimax])

            for middle_pos, end_pos, read_len, num_mapped, _ in ss_group:
                if num_mapped > multimax:
                    continue
                grp_pos = (xlink_pos, middle_pos, end_pos)[gi]
                w = read_len / (num_mapped * sum_len_per_barcode)

                current_values = counts.get(grp_pos, (0, 0))
                upadated_values = (current_values[0] + w, current_values[1] + 1)
                counts[grp_pos] = upadated_values

    return counts


def _intersects_with_annotaton(second_start, annotation, chrom, strand):
    """
    Does a second_start corresopnd to any entry in annotation?

    Returns
    -------
        bool
    Does the read's second_start corresopnd to any known segment in annotation
    """
    for gene_id, gene_content in annotation[(chrom, strand)].items():
        for transcript_id, transcript_content in gene_content.items():
            if transcript_id == 'gene_segment':
                continue
            for segment in transcript_content:
                if strand == '+':
                    if second_start == segment.start:
                        return True
                else:
                    if second_start == segment.stop:
                        return True
    return False


def _second_start(read, poss, strange, strand, chrom, annotation, holesize_th):
    """
    Return the coordinate of second start. If read is not split or we wish algorithm
    to think of read as linear, second_start equals to 0.

    """
    holes = [j-i-1 for i, j in zip(poss, poss[1:])]
    # Get the size of the biggest hole:
    biggest_hole_size = max(holes) if holes else 0

    second_start = 0
    if not annotation:
        # Effectively this means, that read is considered as it has no holes.
        if biggest_hole_size > holesize_th:
            # Still, read is not treated as on with distinct second_start.
            # However, it is reported as starnge:
            strange.append(read)
    else:
        biggest_hole_size_index = holes.index(biggest_hole_size)
        # Take right border of hole on "+" and left border on "-" strand:
        if strand == '+':
            second_start = poss[biggest_hole_size_index + 1]
        else:
            second_start = poss[biggest_hole_size_index]

        if not _intersects_with_annotaton(second_start, annotation, chrom, strand):
            strange.append(read)
            second_start = 0

    return second_start


def _processs_bam_file(bam_fname, metrics, mapq_th, sites_strange, annotation=None, holesize_th=4):
    """
    Extract data from BAM file into dictionary.

    The structire of dictionary is the following::

        grouped = {
            ('chr1', '+'): (
                xlink_pos1: {
                    (barcode1, [
                        (middle_pos, end_pos, read_len, num_mapped, cigar, second_start)
                        ...
                    ]),
                    (barcode2, [
                        (middle_pos, end_pos, read_len, num_mapped, cigar, second_start)
                        ...
                    ]),
                    ....
                }),
            ),
        }

    Parameters
    ----------
    bam_fname : str
        BAM file with mapped reads.
    metrics : iCount.Metrics
        Metrics object for storing analysis metadata.
    mapq_th : int
        Ignore hits with MAPQ < mapq_th.
    sites_strange : str
        Output BED6 file to store reads with strange properties. If read's
        second start does not fall on any of annotation borders, it is
        considered strange. If segmentation is not provided, every read in two
        parts, longer than holesize_th is considered as strange.
    annotation : str
        File with custon annotation format (obtained by ``iCount segment``).
    holesize_th : int
        Raeads with size of holes less than holesize_th are treted as if they
        would have no holes.

    Returns
    -------
    dict
        Internal structure of BAM file, described in docstring.
    list
        BAM file with
    """
    # Process annotation, if given:
    if annotation:
        annotation = iCount.genomes.segment._prepare_annotation(annotation)

    try:
        bamfile = pysam.AlignmentFile(bam_fname, 'rb')
    except OSError as e:
        raise ValueError('Error opening BAM file: {:s}'.format(bam_fname))

    # counters
    metrics.all_recs = 0  # All records
    metrics.notmapped_recs = 0  # Not mapped records
    metrics.mapped_recs = 0  # Mapped records
    metrics.lowmapq_recs = 0  # Records with insufficient quality
    metrics.used_recs = 0  # Records used in analysis (all - unmapped - lowmapq)
    metrics.invalidrandomer_recs = 0  # Records with invalid randomer
    metrics.norandomer_recs = 0  # Records with no randomer
    metrics.bc_cn = {}  # Barcode counter

    # root container for reads (grouped by chromosome, strand, position and random barcode)
    grouped = {}
    # Container for reads with strange properties:
    strange = []

    _cache_bcs = {}
    for r in bamfile:
        metrics.all_recs += 1
        if r.is_unmapped:
            metrics.notmapped_recs += 1
            continue

        metrics.mapped_recs += 1

        if r.mapq < mapq_th:
            metrics.lowmapq_recs += 1
            continue

        metrics.used_recs += 1

        # NH (number of reported alignments) tag is required:
        if r.has_tag('NH'):
            num_mapped = r.get_tag('NH')
        else:
            raise ValueError('"NH" tag not set for record: {}'.format(r.query_name))

        # Extract randomer sequence (``bc``) from querry name (= read name)
        bc = _get_random_barcode(r.query_name, metrics)
        bc = _cache_bcs.setdefault(bc, bc)  # reduce memory consumption
        metrics.bc_cn[bc] = metrics.bc_cn.get(bc, 0) + 1

        # position of cross-link is one nucleotide before start of read
        poss = sorted(r.positions)
        if r.is_reverse:
            strand = '-'
            xlink_pos = poss[-1] + 1
            end_pos = poss[0]
        else:
            strand = '+'
            xlink_pos = poss[0] - 1
            end_pos = poss[-1]
        chrom = bamfile.references[r.tid]

        # middle position is position of middle nucleotide. Because we can have
        # spliced reads, middle position is not necessarily the middle of
        # region the read maps to.
        i = len(poss)
        if i % 2 == 0:
            i = i // 2
            if strand == '-':
                # take one nucleotide upstream of center, happens by default
                # on + strand
                i = i - 1
        else:
            i = i // 2
        middle_pos = poss[i]
        read_len = len(r.seq)

        second_start = _second_start(r, poss, strange, strand, chrom, annotation, holesize_th)

        read_data = (middle_pos, end_pos, read_len, num_mapped, second_start)

        # store hit data in a "grouped" contianer:
        grouped.setdefault((chrom, strand), {}).\
            setdefault(xlink_pos, {}).\
            setdefault(bc, []).\
            append(read_data)

    # Write strange behaved reads to a BAM file:
    metrics.strange_recs = len(strange)
    if strange:
        with pysam.AlignmentFile(sites_strange, "w", header=bamfile.header) as outf:
            for read in strange:
                outf.write(read)

    bamfile.close()

    # Report:
    LOGGER.info('All records in BAM file: %d', metrics.all_recs)
    LOGGER.info('Reads not mapped: %d', metrics.notmapped_recs)
    LOGGER.info('Mapped reads records (hits): %d', metrics.mapped_recs)
    LOGGER.info('Hits ignored because of low MAPQ: %d', metrics.lowmapq_recs)
    LOGGER.info('Records used for quantification: %d', metrics.used_recs)
    LOGGER.info('Records with invalid randomer info in header: %d', metrics.invalidrandomer_recs)
    LOGGER.info('Records with no randomer info: %d', metrics.norandomer_recs)
    LOGGER.info('Ten most frequent randomers:')
    top10 = sorted([(cn, bc) for bc, cn in metrics.bc_cn.items()], reverse=True)[:10]
    for cn, bc in top10:
        LOGGER.info('    %s: %d', bc, cn)
    LOGGER.info('There are {} reads with second-start not falling on annotation. They are '
                'reported in file: {}'.format(metrics.strange_recs, sites_strange))

    return grouped


def run(bam, sites_unique, sites_multi, sites_strange, group_by='start', quant='cDNA',
        segmentation=None, mismatches=2, mapq_th=0, multimax=50, holesize_th=4,
        report_progress=False):

    """
    Interpret mapped sites and generate BED file with coordinates and
    number of cross-linked events.

    MAPQ is calculated mapq=int(-10*log10(1-1/Nmap)). By default we set
    the mapq_th to 0 to include all reads. Mapq score is very useful,
    because values coming from STAR are from a very limited set: 0 (5 or
    more multiple hits), 1 (4 or 3 multiple hits), 3 (2 multiple hits),
    255 (unique hit)

    Parameters
    ----------
    bam : str
        Input BAM file with mapped reads.
    sites_unique : str
        Output BED6 file to store data from uniquely mapped reads.
    sites_multi : str
        Output BED6 file to store data from multi-mapped reads.
    sites_strange : str
        Output BAM file to store reads with strange properties. If read's
        second start does not fall on any of annotation borders, it is
        considered strange. If segmentation is not provided, every read in two
        parts, longer than holesize_th is considered as strange.
    group_by : str
        Assign score of a read to either 'start', 'middle' or 'end' nucleotide.
    quant : str
        Report number of 'cDNA' or number of 'reads'.
    mismatches : int
        Reads on same position with random barcode differing less than
        ``mismatches`` are grouped together.
    segmentation : str
        File with custon annotation format (obtained by ``iCount segment``).
    mapq_th : int
        Ignore hits with MAPQ < mapq_th.
    multimax : int
        Ignore reads, mapped to more than ``multimax`` places.
    report_progress : bool
        Switch to report progress.
    holesize_th : int
        Raeads with size of holes less than holesize_th are treted as if they
        would have no holes.

    Returns
    -------
    iCount.Metrics
        Metrics object, storing analysis metadata.

    """
    iCount.log_inputs(LOGGER, level=logging.INFO)

    assert sites_unique.endswith(('.bed', '.bed.gz'))
    assert sites_multi.endswith(('.bed', '.bed.gz'))
    assert sites_strange.endswith(('.bam', '.bam.gz'))
    assert quant in ['cDNA', 'reads']
    assert group_by in ['start', 'middle', 'end']

    metrics = iCount.Metrics()
    LOGGER.info('Processing BAM file to internal structure...')
    grouped = _processs_bam_file(bam, metrics, mapq_th, sites_strange, segmentation, holesize_th)

    LOGGER.info('Detecting cross-links...')
    unique, multi = {}, {}
    length = len(grouped)
    progress = 0
    for (chrom, strand), by_pos in grouped.items():
        if report_progress:
            new_progress = 1 - len(grouped) / length
            progress = iCount._log_progress(new_progress, progress, LOGGER)

        unique_by_pos = {}
        multi_by_pos = {}
        for xlink_pos, by_bc in by_pos.items():

            _merge_similar_randomers(by_bc, mismatches)

            # count uniquely mapped reads only
            _update(unique_by_pos, _collapse(xlink_pos, by_bc, group_by, multimax=1))
            # count all reads mapped les than multimax times
            _update(multi_by_pos, _collapse(xlink_pos, by_bc, group_by, multimax=multimax))

        unique[(chrom, strand)] = unique_by_pos
        multi[(chrom, strand)] = multi_by_pos

    # Write output
    val_index = ['cDNA', 'reads'].index(quant)
    iCount.files.bed.save_dict(unique, sites_unique, val_index=val_index)
    LOGGER.info('Saved to BED file (uniquely mapped reads): %s', sites_unique)
    iCount.files.bed.save_dict(multi, sites_multi, val_index=val_index)
    LOGGER.info('Saved to BED file (multi-mapped reads): %s', sites_multi)

    return metrics
